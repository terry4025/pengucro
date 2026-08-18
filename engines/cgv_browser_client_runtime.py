from __future__ import annotations

from typing import Any, Callable

from engines import browser_session
from engines.cgv_browser_client import CgvBrowserClient as BaseCgvBrowserClient
from engines.cgv_client import CGV_HOME_URL, CgvError


class CgvBrowserClient(BaseCgvBrowserClient):
    """CGV browser client that reuses one persistent CGV tab whenever possible.

    The base client opened a fresh tab for every catalog/schedule/seat read and
    closed it afterwards.  That was safe but noisy and made a user who had
    already logged in feel as if the app was starting a new CGV session for each
    action.  This runtime keeps the persistent Chrome/profile behavior but
    prefers an already-open CGV tab in the same context.  Only when no usable
    CGV tab exists does it create one, and that page is intentionally left open
    for the next operation.
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

    def _with_page(self, operation: Callable[[Any], Any]):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CgvError("CGV 데이터 조회에 필요한 브라우저 모듈을 찾지 못했습니다.") from exc

        last_error: Exception | None = None
        for attempt in range(2):
            chrome = browser_session.start_isolated(log=self._emit)
            if chrome is None:
                raise CgvError("CGV 데이터 조회용 Chrome을 시작하지 못했습니다.")
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
                    return result
            except Exception as exc:
                last_error = exc
                if attempt == 0 and self._is_recoverable_browser_error(exc):
                    self._emit("[CGV] 브라우저 연결 끊김 · 1회 자동 복구", "warning")
                    continue
                if attempt > 0:
                    self._emit("[CGV] 브라우저 자동 복구 실패", "error")
                raise
            finally:
                # Release only the Pengucro slot lease.  The persistent Chrome
                # process and the selected CGV tab remain alive and keep the
                # authenticated profile/session for the next operation.
                chrome.release()
        if last_error:
            raise last_error
