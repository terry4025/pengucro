from __future__ import annotations

import time

from engines.cgv_engine_guarded import CgvEngine as GuardedCgvEngine
from engines.cgv_client import CGV_HOME_URL


class CgvEngine(GuardedCgvEngine):
    """Production CGV runtime policy layered over the guarded engine.

    Keep the existing seat/hold fast path intact while tightening the edges of
    the end-to-end flow:

    * target-date schedule polling no longer sleeps for as long as 20 seconds
      while the date is completely unpublished;
    * CGV's intermediate "결제 전 확인" sheet is acknowledged before waiting
      for the payment-method page;
    * a valid login already present in the persistent Chrome profile is reused
      immediately instead of navigating that tab through /mem/login again.

    The schedule cadence remains bounded and the base engine's existing
    403/429 backoff still reduces concurrency and slows retries when CGV asks us
    to back off.
    """

    PREOPEN_IDLE_INTERVAL = 2.0
    SCHEDULE_HINT_INTERVAL = 1.0

    @staticmethod
    def _context_has_member_session(context) -> bool:
        try:
            return any(
                str(cookie.get("name", "")) in {"accessToken", "refresh_token"}
                and bool(cookie.get("value"))
                for cookie in context.cookies(CGV_HOME_URL)
            )
        except Exception:
            return False

    def _ensure_member_session(self, page, context) -> bool:
        # The historical verifier always navigated to /mem/login first and used
        # CGV's redirect as the session check.  That visibly replaced a tab the
        # user had already logged into and could trigger a needless login flow.
        # A valid first-party CGV auth cookie in the same persistent context is
        # enough to reuse the session; the normal booking request remains the
        # final authority and the base login flow is retained when no cookie is
        # available.
        if self._context_has_member_session(context):
            self.log(
                "[CGV] 기존 Chrome의 로그인 세션 확인 · 현재 CGV 탭을 그대로 재사용합니다.",
                "success",
            )
            return True
        return super()._ensure_member_session(page, context)

    @staticmethod
    def _click_checkout_confirmation(page) -> bool:
        """Click only CGV's intermediate pre-payment confirmation button."""

        try:
            result = page.evaluate(
                r"""
                () => {
                  const clean = value => (value || '').replace(/\s+/g, '');
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const candidates = Array.from(document.querySelectorAll(
                    'button, a, [role="button"]'
                  )).filter(node => {
                    const text = clean(node.innerText || node.textContent);
                    return visible(node) && text === '결제하기' &&
                           !node.disabled && node.getAttribute('aria-disabled') !== 'true';
                  });

                  for (const button of candidates) {
                    let scope = button;
                    for (let depth = 0; scope && depth < 10; depth += 1, scope = scope.parentElement) {
                      if (scope === document.body || scope === document.documentElement) break;
                      const text = clean(scope.innerText || scope.textContent);
                      const confirmationTitle = text.includes('결제전확인');
                      const confirmationDetails =
                        text.includes('취소/환불') || text.includes('입장시유의사항') ||
                        text.includes('상영관입장');
                      if (!confirmationTitle || !confirmationDetails) continue;
                      if (typeof button.scrollIntoView === 'function') {
                        button.scrollIntoView({block: 'center', inline: 'center'});
                      }
                      button.click();
                      return true;
                    }
                  }
                  return false;
                }
                """
            )
            return bool(result)
        except Exception:
            return False

    def _advance_to_cgv_payment_methods(self, page) -> bool:
        """Advance through the seat summary and the CGV confirmation sheet."""

        if self._cgv_payment_methods_ready(page):
            return True

        clicked, _text = self._wait_and_click_payment_button(
            page,
            self.NPAY_CONTROL_TIMEOUT_SECONDS,
        )
        if not clicked:
            self.log(
                "CGV 좌석 확인 화면의 첫 번째 '결제하기' 버튼을 찾지 못했습니다.",
                "warning",
            )
            return False

        self.log("[CGV] 좌석 확인 완료 · 결제 전 안내 및 결제수단 화면 확인 중...", "info")

        deadline = time.monotonic() + self.CGV_PAYMENT_PAGE_TIMEOUT_SECONDS
        confirmation_clicked = False
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if self._cgv_payment_methods_ready(page):
                return True

            if not confirmation_clicked and self._click_checkout_confirmation(page):
                confirmation_clicked = True
                self.log(
                    "[CGV] 결제 전 확인 안내 확인 · 안내창의 '결제하기' 클릭 완료",
                    "info",
                )

            try:
                page.wait_for_timeout(self.PAYMENT_POLL_INTERVAL_MS)
            except Exception:
                if self.stop_event.wait(self.PAYMENT_POLL_INTERVAL_MS / 1000.0):
                    break

        ready = self._cgv_payment_methods_ready(page)
        if not ready:
            detail = (
                "결제 전 확인 안내는 처리했지만 "
                if confirmation_clicked
                else "결제 전 확인 안내 또는 "
            )
            self.log(
                f"CGV {detail}결제수단 화면(/mpy/main) 진입을 확인하지 못했습니다.",
                "warning",
            )
        return ready

    def log(self, message: str, level: str = "info") -> None:
        if message == "[CGV] 미오픈 대기 · 20초 간격으로 시간표 확인":
            message = (
                f"[CGV] 미오픈 대기 · {self.PREOPEN_IDLE_INTERVAL:g}초 간격으로 "
                "목표 날짜 시간표 확인"
            )
        elif message == "[CGV] 목표 영화 선공개 감지 · 감시 간격 단축 (2초)":
            message = (
                f"[CGV] 목표 영화 선공개 감지 · 감시 간격 "
                f"{self.SCHEDULE_HINT_INTERVAL:g}초로 단축"
            )
        super().log(message, level)
