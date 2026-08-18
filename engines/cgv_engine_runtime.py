from __future__ import annotations

import time

import engines.cgv_engine as _base_cgv_engine
from engines.cgv_chrome_session import CgvBrowserSessionProxy
from engines.cgv_engine_guarded import CgvEngine as GuardedCgvEngine
from engines.cgv_client import CGV_HOME_URL


# The historical base engine calls its module-level ``browser_session`` from
# initial launch and reconnect paths. Replace only that CGV module reference;
# other engines continue to use the generic first-free-slot allocator.
if not isinstance(_base_cgv_engine.browser_session, CgvBrowserSessionProxy):
    _base_cgv_engine.browser_session = CgvBrowserSessionProxy()


class CgvEngine(GuardedCgvEngine):
    """Final CGV runtime policy.

    Keep the guarded seat/hold path intact while tightening the last-mile state
    transitions:

    * target-date schedule polling remains fast but bounded;
    * CGV's intermediate checkout confirmation is acknowledged;
    * an existing member session is reused;
    * every CGV operation stays on persistent Chrome slot 1 / port 9333;
    * when several seat priorities are configured, the group that actually wins
      becomes authoritative and stale selections from other groups are cleared
      before ``선택완료`` is submitted.
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
        if self._context_has_member_session(context):
            self.log(
                "[CGV] 슬롯 1의 기존 Chrome 로그인 세션 확인 · 현재 CGV 탭을 그대로 재사용합니다.",
                "success",
            )
            return True
        return super()._ensure_member_session(page, context)

    @staticmethod
    def _serialize_structured_seat_groups(value, people: int) -> str:
        groups: list[str] = []
        if not isinstance(value, (list, tuple)):
            return ""
        expected = max(1, int(people))
        for raw_group in value:
            if not isinstance(raw_group, (list, tuple)):
                continue
            seats = [str(seat or "").strip() for seat in raw_group]
            seats = [seat for seat in seats if seat]
            if len(seats) != expected:
                continue
            groups.append(",".join(seats))
        return " | ".join(groups)

    def make_reservation_thread(self, reservation_data: dict) -> None:
        """Keep the dialog's structured priority list authoritative to the engine."""

        data = dict(reservation_data or {})
        metadata = dict(data.get("engine_metadata", {}) or {})
        cgv = dict(metadata.get("cgv", {}) or {})
        people = max(1, int(data.get("people", 1) or 1))
        structured = self._serialize_structured_seat_groups(
            cgv.get("seat_groups"), people
        )
        if structured:
            # The mature base engine still consumes CgvSeatGroup objects. Keep
            # the structured list authoritative and serialize only once at this
            # compatibility boundary instead of losing it in the form layer.
            cgv["seats"] = structured
            metadata["cgv"] = cgv
            data["engine_metadata"] = metadata
        return super().make_reservation_thread(data)

    @staticmethod
    def _exact_seat_selection_snapshot(page, target_ids: list[str]) -> dict:
        try:
            result = page.evaluate(
                r"""
                targetIds => {
                  const target = new Set(targetIds.map(String));
                  const clean = value => String(value || '').replace(/\s+/g, '');
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const isSelected = node => {
                    const classes = String(node.className || '').toLowerCase();
                    const tokens = new Set(classes.split(/[\s_\-]+/));
                    return node.title === '선택됨' ||
                           node.getAttribute('aria-pressed') === 'true' ||
                           node.getAttribute('aria-selected') === 'true' ||
                           tokens.has('selected') || tokens.has('active') || tokens.has('on');
                  };
                  const selectedIds = Array.from(
                    document.querySelectorAll('button[data-seatlocno]')
                  ).filter(node => visible(node) && isSelected(node))
                   .map(node => String(node.getAttribute('data-seatlocno') || ''))
                   .filter(Boolean);
                  const selectedSet = new Set(selectedIds);
                  const extras = selectedIds.filter(id => !target.has(id));
                  const missing = targetIds.map(String).filter(id => !selectedSet.has(id));
                  const submit = Array.from(
                    document.querySelectorAll('button, a, div[role="button"]')
                  ).find(node => clean(node.textContent) === '선택완료' && visible(node));
                  const submitReady = Boolean(
                    submit && !submit.disabled && submit.getAttribute('aria-disabled') !== 'true'
                  );
                  return {
                    selectedIds,
                    extras,
                    missing,
                    submitPresent: Boolean(submit),
                    submitReady,
                    ready: extras.length === 0 && missing.length === 0 &&
                           selectedIds.length === target.size && submitReady,
                  };
                }
                """,
                [str(value) for value in target_ids],
            )
            return dict(result) if isinstance(result, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _apply_exact_seat_selection(page, target_ids: list[str]) -> bool:
        """Make one priority group the only selected group in CGV's seat modal."""

        try:
            result = page.evaluate(
                r"""
                targetIds => {
                  const target = new Set(targetIds.map(String));
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const isSelected = node => {
                    const classes = String(node.className || '').toLowerCase();
                    const tokens = new Set(classes.split(/[\s_\-]+/));
                    return node.title === '선택됨' ||
                           node.getAttribute('aria-pressed') === 'true' ||
                           node.getAttribute('aria-selected') === 'true' ||
                           tokens.has('selected') || tokens.has('active') || tokens.has('on');
                  };
                  const unavailable = node => {
                    const classes = String(node.className || '').toLowerCase();
                    const tokens = new Set(classes.split(/[\s_\-]+/));
                    return node.disabled || node.getAttribute('aria-disabled') === 'true' ||
                      ['disabled', 'complete', 'sold', 'reserved', 'finish', 'soldout']
                        .some(key => tokens.has(key) || classes.includes(key));
                  };
                  const nodes = Array.from(
                    document.querySelectorAll('button[data-seatlocno]')
                  ).filter(visible);
                  let acted = false;

                  // First remove any stale selection left by a different
                  // priority attempt. This is what keeps the visitor count and
                  // React's selected-seat count in sync.
                  for (const node of nodes) {
                    const id = String(node.getAttribute('data-seatlocno') || '');
                    if (id && isSelected(node) && !target.has(id)) {
                      node.click();
                      acted = true;
                    }
                  }

                  // Then select only the active group's seats.
                  for (const id of targetIds.map(String)) {
                    const node = nodes.find(item =>
                      String(item.getAttribute('data-seatlocno') || '') === id
                    );
                    if (!node || unavailable(node)) return {ok: false, acted};
                    if (!isSelected(node)) {
                      if (typeof node.scrollIntoView === 'function') {
                        node.scrollIntoView({block: 'center', inline: 'center'});
                      }
                      node.click();
                      acted = true;
                    }
                  }
                  return {ok: true, acted};
                }
                """,
                [str(value) for value in target_ids],
            )
            return bool(isinstance(result, dict) and result.get("ok"))
        except Exception:
            return False

    def _normalize_active_seat_group(self, page, seat_ids: list[str]) -> bool:
        target_ids = [str(value or "") for value in seat_ids if str(value or "")]
        if not target_ids:
            return False

        observed_snapshot = False
        cleaned_extras = False
        for attempt in range(self.API_UI_SYNC_ATTEMPTS):
            snapshot = self._exact_seat_selection_snapshot(page, target_ids)
            if snapshot:
                observed_snapshot = True
                if snapshot.get("ready"):
                    if cleaned_extras:
                        self.log(
                            "[CGV] 활성 좌석 우선순위만 남기도록 이전 선택 상태를 정리했습니다.",
                            "info",
                        )
                    return True
                if snapshot.get("extras"):
                    cleaned_extras = True

            # Give React a few frames between corrective clicks so one click is
            # not accidentally toggled twice while state propagation is pending.
            if attempt == 0 or attempt % 4 == 0:
                if not self._apply_exact_seat_selection(page, target_ids):
                    if snapshot and snapshot.get("missing"):
                        return False

            try:
                page.wait_for_timeout(self.API_UI_SYNC_INTERVAL_MS)
            except Exception:
                time.sleep(self.API_UI_SYNC_INTERVAL_MS / 1000.0)

        # Preserve legacy mocked-page compatibility when no DOM snapshot can be
        # observed; the submit helper still checks the enabled button itself.
        return not observed_snapshot

    def _select_api_seats_in_ui(self, page, payload, selected) -> bool:
        self._sync_seat_payload_to_ui(page, payload)
        seat_ids: list[str] = []
        for seat in selected:
            seat_id = getattr(seat, "seat_id", None) or (
                seat.get("seat_id") or seat.get("seatLocNo") or seat.get("id")
                if isinstance(seat, dict)
                else str(seat)
            )
            seat_id = str(seat_id or "")
            if not seat_id:
                return False
            seat_ids.append(seat_id)
        return self._normalize_active_seat_group(page, seat_ids)

    def _wait_for_seat_selection_ready(self, page, seat_ids: list[str]) -> bool:
        # Browser fallback used to only ensure that target seats were selected.
        # With multiple priorities that could leave an earlier group's seat in
        # React state, keeping 선택완료 disabled. Normalize to exactly one group.
        return self._normalize_active_seat_group(page, seat_ids)

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
