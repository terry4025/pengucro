from __future__ import annotations

from typing import Any, Callable

from engines import browser_session
from engines.cgv_browser_client import CgvBrowserClient as BaseCgvBrowserClient
from engines.cgv_client import CGV_HOME_URL, CgvError


class CgvBrowserClient(BaseCgvBrowserClient):
    """CGV browser client that reuses one persistent CGV tab whenever possible.

    Successful operations keep the selected CGV tab alive. If Playwright/CDP
    reports a recoverable disconnect, that page is no longer a safe reuse
    candidate, so it is closed before the one supported reconnect attempt.
    """

    @staticmethod
    def _page_usable(page: Any) -> bool:
        if page is None:
            return False
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        return True

    def _pick_existing_cgv_page(self, context: Any):
        try:
            pages = list(context.pages)
        except Exception:
            return None
        for page in reversed(pages):
            if not self._page_usable(page):
                continue
            try:
                if "cgv.co.kr" in str(page.url or "").lower():
                    return page
            except Exception:
                continue
        return None

    @staticmethod
    def _discard_broken_page(page: Any) -> None:
        if page is None:
            return
        try:
            page.close()
        except Exception:
            pass

    def _with_page(self, operation: Callable[[Any], Any]):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CgvError("CGV 데이터 조회에 필요한 브라우저 모듈을 찾지 못했습니다.") from exc

        last_error: Exception | None = None
        broken_page = None
        for attempt in range(2):
            chrome = browser_session.start_isolated(log=self._emit)
            if chrome is None:
                raise CgvError("CGV 데이터 조회용 Chrome을 시작하지 못했습니다.")
            page = None
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.connect_over_cdp(chrome.endpoint)
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = self._pick_existing_cgv_page(context)
                    if page is None:
                        page = context.new_page()
                        self._goto_with_retry(
                            page,
                            CGV_HOME_URL,
                            wait_until="domcontentloaded",
                            timeout=45000,
                        )
                        self._emit(
                            "[CGV] 재사용할 탭이 없어 CGV 탭 1개를 준비했습니다. 이후 조회에서 그대로 재사용합니다.",
                            "info",
                        )
                    else:
                        self._emit(
                            "[CGV] 이미 열려 있는 CGV 탭과 로그인 세션을 그대로 재사용합니다.",
                            "info",
                        )

                    result = operation(page)
                    if attempt > 0:
                        self._emit("[CGV] 브라우저 자동 복구 성공", "success")

                    # A reconnect should normally produce a fresh Page object.
                    # If a browser/mock context hands back the exact object that
                    # was already classified as disconnected, do not leave that
                    # object in the reusable-tab pool even if this operation
                    # happened to complete. Healthy fresh pages remain open.
                    if broken_page is not None and page is broken_page:
                        self._discard_broken_page(page)
                    return result
            except Exception as exc:
                last_error = exc
                recoverable = self._is_recoverable_browser_error(exc)
                if recoverable:
                    # A disconnected target must not survive in the reusable-tab
                    # pool. Remember its identity so a reconnect cannot silently
                    # recycle the same object as a supposedly healthy tab.
                    broken_page = page
                    self._discard_broken_page(page)
                    if attempt == 0:
                        self._emit("[CGV] 브라우저 연결 끊김 · 손상 탭 정리 후 1회 자동 복구", "warning")
                        continue
                if attempt > 0 and recoverable:
                    self._emit("[CGV] 브라우저 자동 복구 실패", "error")
                raise
            finally:
                # Keep healthy tabs/profile alive across normal reads; only a
                # recoverable failure (or recycled broken object) is closed.
                chrome.release()
        if last_error:
            raise last_error
