from __future__ import annotations

import time
from typing import Any

from engines import browser_session
from engines.cgv_engine import CgvEngine as BaseCgvEngine
from engines.cgv_client import CGV_HOME_URL, CgvSeatGroup
from pengucro.diagnostics import format_exception


class CgvEngine(BaseCgvEngine):
    """CGV engine with recovery hardening layered over the main implementation.

    The base engine owns the normal booking flow.  This subclass deliberately
    keeps the recovery-specific fixes small: it tracks the page that survived a
    CDP recovery, keeps Playwright browser/context handles synchronized, restarts
    a dead persistent Chrome profile when its endpoint is gone, and avoids
    treating ordinary selected-seat summary text as a seat-conflict alert.
    """

    def __init__(self, log_callback, success_callback=None, **kwargs) -> None:
        super().__init__(log_callback, success_callback, **kwargs)
        self._active_page = None

    @staticmethod
    def _page_usable(page) -> bool:
        if page is None:
            return False
        try:
            if hasattr(page, "is_closed") and page.is_closed():
                return False
        except Exception:
            return False
        try:
            context = getattr(page, "context", None)
            browser = getattr(context, "browser", None) if context is not None else None
            if browser is not None and hasattr(browser, "is_connected"):
                if not browser.is_connected():
                    return False
        except Exception:
            return False
        return True

    def _sync_runtime_handles_from_page(self, page) -> None:
        if not self._page_usable(page):
            return
        self._active_page = page
        try:
            context = page.context
            self._context = context
            browser = getattr(context, "browser", None)
            if browser is not None:
                self._browser = browser
        except Exception:
            pass

    def _current_page(self, fallback=None):
        # Prefer the page explicitly handed to the current operation when it is
        # alive.  This is important while a reconnect helper is constructing a
        # brand-new page.  Fall back to the last recovered page only when the
        # caller is still holding a closed/disconnected page object.
        if self._page_usable(fallback):
            self._sync_runtime_handles_from_page(fallback)
            return fallback
        if self._page_usable(self._active_page):
            return self._active_page
        return fallback if fallback is not None else self._active_page

    def _race_schedule(self, page, url: str, concurrency: int) -> dict[str, Any]:
        self._sync_runtime_handles_from_page(page)
        return super()._race_schedule(page, url, concurrency)

    def _enter_visitor_page(self, page, schedule: dict[str, Any]) -> bool:
        target = self._current_page(page)
        if target is None:
            return False
        ok = super()._enter_visitor_page(target, schedule)
        if ok:
            self._sync_runtime_handles_from_page(target)
        return ok

    def _select_visitors(self, page, people: int) -> bool:
        target = self._current_page(page)
        if target is None:
            return False
        ok = super()._select_visitors(target, people)
        if ok:
            self._sync_runtime_handles_from_page(target)
        return ok

    def _watch_and_hold_api(
        self,
        page,
        schedule: dict[str, Any],
        groups: tuple[CgvSeatGroup, ...],
        people: int,
        developer_mode: bool,
        cgv: dict[str, Any],
    ) -> tuple[bool, bool]:
        target = self._current_page(page)
        if target is None:
            return False, True
        self._sync_runtime_handles_from_page(target)
        return super()._watch_and_hold_api(
            target, schedule, groups, people, developer_mode, cgv
        )

    def _restore_fetch(self, page) -> None:
        target = self._current_page(page)
        if target is not None:
            BaseCgvEngine._restore_fetch(target)

    def _reload_or_recover_seat_page(
        self,
        page,
        schedule: dict[str, Any] | None = None,
        people: int = 1,
    ) -> tuple[Any, bool]:
        target = self._current_page(page)
        returned_page, ok = super()._reload_or_recover_seat_page(
            target, schedule=schedule, people=people
        )
        if self._page_usable(returned_page):
            self._sync_runtime_handles_from_page(returned_page)
        return returned_page, ok

    def _select_and_hold_seats(
        self,
        page,
        groups: tuple[CgvSeatGroup, ...],
        people: int,
        developer_mode: bool,
        schedule: dict[str, Any] | None = None,
    ) -> bool:
        target = self._current_page(page)
        if target is None:
            return False
        held = super()._select_and_hold_seats(
            target,
            groups,
            people,
            developer_mode,
            schedule=schedule,
        )
        # _reload_or_recover_seat_page() updates _active_page whenever the base
        # loop swaps to a recovered page.  The checkout override below will use
        # that page even though the base make_reservation_thread still holds its
        # original local variable.
        return held

    def _proceed_naver_pay_checkout(self, page, developer_mode: bool = False) -> bool:
        target = self._current_page(page)
        if target is None:
            self.log(
                "CGV 결제 단계에서 사용할 브라우저 페이지가 닫혀 수동 확인이 필요합니다.",
                "warning",
            )
            return True
        return super()._proceed_naver_pay_checkout(
            target, developer_mode=developer_mode
        )

    def _reconnect_seat_session(
        self,
        schedule: dict[str, Any] | None = None,
        people: int = 1,
    ) -> Any:
        # First let the base implementation reconnect to the browser/context we
        # already know.  This is the fastest path for a page-only closure or a
        # transient Playwright CDP disconnect.
        page = super()._reconnect_seat_session(schedule, people)
        if self._page_usable(page):
            self._sync_runtime_handles_from_page(page)
            return page

        chrome = getattr(self, "_chrome", None)
        port = getattr(chrome, "port", 0) if chrome is not None else 0
        endpoint_alive = bool(port and browser_session.cdp_descriptor(port))
        if endpoint_alive:
            # The endpoint itself is alive, so a blind process restart would be
            # more destructive than useful.  Preserve the browser for manual
            # inspection and let the caller report the failed recovery.
            return None

        # A ChromeSession object can retain an endpoint string after the actual
        # process has died.  Release its slot before asking start_isolated() for
        # a replacement so the same persistent profile/port can be reacquired.
        if chrome is not None:
            try:
                chrome.release()
            except Exception:
                pass

        self.log(
            "[CGV] 기존 Chrome DevTools endpoint가 종료되어 같은 프로필로 Chrome을 다시 시작합니다.",
            "warning",
        )
        fresh = browser_session.start_isolated(log=self.log)
        if fresh is None:
            return None

        # The base make_reservation_thread owns only the original local Chrome
        # object.  Arrange for the replacement lease to be released when its
        # browser is eventually closed so a recovered slot cannot leak.
        self._release_browser_lease_when_closed(fresh)
        self._chrome = fresh
        self._browser = None
        self._context = None
        self._active_page = None

        playwright = getattr(self, "_playwright", None)
        if playwright is None:
            return None

        try:
            browser = playwright.chromium.connect_over_cdp(fresh.endpoint)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = next(
                (
                    item
                    for item in context.pages
                    if not item.is_closed() and "cgv.co.kr" in item.url
                ),
                None,
            )
            page = page or context.new_page()
            page.on("dialog", lambda dialog: dialog.accept())

            self._browser = browser
            self._context = context
            self._active_page = page

            if schedule:
                if not BaseCgvEngine._enter_visitor_page(self, page, schedule):
                    return None
            else:
                page.goto(
                    f"{CGV_HOME_URL}/cnm/selectVisitorCnt",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            if not BaseCgvEngine._select_visitors(self, page, people):
                return None

            self._sync_runtime_handles_from_page(page)
            self.log(
                "[CGV] Chrome 프로세스 재시작 및 좌석 화면 복구 성공 · 좌석 선택을 계속합니다.",
                "success",
            )
            return page
        except Exception as exc:
            self.log(
                f"[CGV] Chrome 프로세스 재시작 후 좌석 화면 복구 실패: {format_exception(exc)}",
                "warning",
            )
            return None

    def _submit_seat_selection(self, page) -> bool:
        """Submit once and distinguish real conflict alerts from normal summaries."""
        target = self._current_page(page)
        if target is None:
            return False

        clicked = False
        try:
            clicked = bool(
                target.evaluate(
                    r"""
                    () => {
                      const clean = s => (s || '').replace(/\s+/g, '');
                      const buttons = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
                      const target = buttons.find(b => clean(b.textContent) === '선택완료' && !b.disabled && b.getAttribute('aria-disabled') !== 'true');
                      if (!target) return false;
                      if (typeof target.scrollIntoView === 'function') target.scrollIntoView({block: 'center'});
                      target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                      target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                      target.click();
                      return true;
                    }
                    """
                )
            )
        except Exception:
            clicked = False

        if not clicked:
            for loc in (
                target.locator("button:has-text('선택완료'), a:has-text('선택완료')"),
                target.get_by_text("선택완료", exact=True),
                target.get_by_text("선택 완료", exact=True),
            ):
                try:
                    for index in range(loc.count()):
                        button = loc.nth(index)
                        if (
                            button.is_visible()
                            and button.is_enabled()
                            and button.get_attribute("aria-disabled") != "true"
                        ):
                            button.click(force=True, timeout=1500)
                            clicked = True
                            break
                    if clicked:
                        break
                except Exception:
                    continue

        if not clicked:
            return False

        def check_conflict() -> bool:
            try:
                return bool(
                    target.evaluate(
                        r"""
                        () => {
                          const visible = el => {
                            const style = window.getComputedStyle(el);
                            return el.offsetParent !== null && style.visibility !== 'hidden' && style.display !== 'none';
                          };
                          const selectors = [
                            '[role="dialog"]', '[role="alert"]', '[aria-modal="true"]',
                            '[class*="modal"]', '[class*="popup"]', '[class*="toast"]', '[class*="alert"]'
                          ];
                          const overlayText = Array.from(document.querySelectorAll(selectors.join(',')))
                            .filter(visible)
                            .map(el => el.innerText || el.textContent || '')
                            .join('\n');
                          const bodyText = document.body ? (document.body.innerText || '') : '';
                          const strongConflictPhrases = [
                            '이미 선택된', '다른 고객이', '예매 중인 좌석', '예매중인 좌석',
                            '선점된 좌석', '이미 예매'
                          ];
                          return strongConflictPhrases.some(phrase =>
                            overlayText.includes(phrase) || bodyText.includes(phrase)
                          );
                        }
                        """
                    )
                )
            except Exception:
                return False

        def check_transition() -> bool:
            try:
                return bool(
                    target.evaluate(
                        r"""
                        () => {
                          const seatButtons = document.querySelectorAll('button[data-seatlocno]');
                          const visibleSeats = Array.from(seatButtons).filter(b => b.offsetParent !== null && !b.hidden);
                          const bodyText = document.body ? (document.body.innerText || '') : '';
                          const hasPaySection = bodyText.includes('결제수단') ||
                                                bodyText.includes('N pay') ||
                                                bodyText.includes('최종 결제금액') ||
                                                bodyText.includes('결제하기');
                          return seatButtons.length === 0 || visibleSeats.length === 0 || hasPaySection;
                        }
                        """
                    )
                )
            except Exception:
                return False

        started = time.monotonic()
        while not self.stop_event.is_set() and time.monotonic() - started < 10.0:
            if check_conflict():
                self._click_visible_by_text(target, ("확인", "닫기", "취소"))
                return False
            if check_transition():
                return True
            try:
                target.wait_for_timeout(150)
            except Exception:
                if self.stop_event.wait(0.15):
                    break

        if check_conflict():
            self._click_visible_by_text(target, ("확인", "닫기", "취소"))
            return False
        return check_transition()
