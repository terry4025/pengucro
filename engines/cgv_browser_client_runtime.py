from __future__ import annotations

from typing import Any, Callable

from engines.cgv_browser_client import CgvBrowserClient as BaseCgvBrowserClient
from engines.cgv_chrome_session import start_cgv_chrome
from engines.cgv_client import CGV_HOME_URL, CgvError


class CgvBrowserClient(BaseCgvBrowserClient):
    """CGV browser client using one persistent slot-1 tab/profile.

    Healthy CGV tabs remain open between selector reads. Recoverable disconnects
    discard only the broken page and retry once on the same persistent slot-1
    profile so CGV/Naver Pay login state is never silently switched to slot 2/3.
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
            chrome = start_cgv_chrome(log=self._emit)
            if chrome is None:
                raise CgvError(
                    "CGV 데이터 조회용 Chrome 슬롯 1을 사용할 수 없습니다. "
                    "슬롯 1을 사용 중인 다른 Pengucro 실행을 먼저 종료해주세요."
                )
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
                            "[CGV] 슬롯 1에 재사용할 CGV 탭이 없어 탭 1개를 준비했습니다. 이후 그대로 재사용합니다.",
                            "info",
                        )
                    else:
                        self._emit(
                            "[CGV] 슬롯 1의 기존 CGV 탭과 로그인 세션을 그대로 재사용합니다.",
                            "info",
                        )

                    result = operation(page)
                    if attempt > 0:
                        self._emit("[CGV] 브라우저 자동 복구 성공", "success")
                    if broken_page is not None and page is broken_page:
                        self._discard_broken_page(page)
                    return result
            except Exception as exc:
                last_error = exc
                recoverable = self._is_recoverable_browser_error(exc)
                if recoverable:
                    broken_page = page
                    self._discard_broken_page(page)
                    if attempt == 0:
                        self._emit(
                            "[CGV] 브라우저 연결 끊김 · 손상 탭 정리 후 슬롯 1에서 1회 자동 복구",
                            "warning",
                        )
                        continue
                if attempt > 0 and recoverable:
                    self._emit("[CGV] 브라우저 자동 복구 실패", "error")
                raise
            finally:
                # Chrome itself remains open. Release only the cross-process
                # lease after this short selector read so the booking engine can
                # immediately reacquire the exact same slot-1 profile.
                chrome.release()
        if last_error:
            raise last_error

    def fetch_schedule_with_reference(self, *args, **kwargs):
        """Never mix a previous-date template into an already-open target day.

        The base helper intentionally gathers recent dates for pre-open seat-map
        references. It also returned those historical template rows alongside
        exact target-date rows, which allowed a 24th selection to accidentally
        carry a 23rd schedule into the seat viewer. When the target date has real
        schedules, expose only those exact rows; historical data remains embedded
        only as ``_pengucroSeatReference`` metadata for layout assistance.
        """

        schedules, reference_date, reference_only = super().fetch_schedule_with_reference(
            *args, **kwargs
        )
        if not reference_only:
            exact = tuple(
                item for item in schedules
                if not bool(item.get("_pengucroPreopen"))
            )
            if exact:
                return exact, reference_date, False
        return schedules, reference_date, reference_only
