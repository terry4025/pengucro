from __future__ import annotations

import time
from typing import Any

from engines.cgv_client import CGV_HOME_URL
from engines.cgv_login import CgvLoginAssistant
from engines.cgv_engine_pairwise_observer import CgvEngine as ObserverCgvEngine


class CgvEngine(ObserverCgvEngine):
    """Final CGV runtime with resilient visitor selection and manual-login resume.

    v6.36 already owns the fast seat hold and observer-accelerated adaptive seat
    synchronization.  This layer only hardens the step immediately before that:
    restoring the target screening route, verifying the visitor-count control,
    opening the official seat modal, and pausing for a manual CGV login when the
    saved browser session has expired.
    """

    VISITOR_HYDRATION_TIMEOUT_SECONDS = 6.0
    VISITOR_COUNT_VERIFY_SECONDS = 0.9
    VISITOR_MODAL_OPEN_TIMEOUT_SECONDS = 4.0
    VISITOR_SEAT_DATA_TIMEOUT_SECONDS = 8.0
    VISITOR_LOCAL_POLL_MS = 60
    LOGIN_LOCAL_POLL_MS = 300

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._visitor_query_payload: dict[str, Any] = {}

    @staticmethod
    def _login_required(page) -> bool:
        try:
            result = page.evaluate(
                r"""
                () => {
                  const path = String(location.pathname || '').toLowerCase();
                  if (path.includes('/mem/login')) return true;
                  const body = String(document.body ? document.body.innerText || '' : '');
                  const compact = body.replace(/\s+/g, '');
                  const hasLoginForm = compact.includes('회원로그인') &&
                    (compact.includes('아이디') || compact.includes('비밀번호'));
                  return hasLoginForm;
                }
                """
            )
            return bool(result)
        except Exception:
            try:
                return "/mem/login" in str(page.url or "").lower()
            except Exception:
                return False

    def _visitor_wait(self, page, milliseconds: int | None = None) -> None:
        delay = self.VISITOR_LOCAL_POLL_MS if milliseconds is None else max(1, int(milliseconds))
        try:
            page.wait_for_timeout(delay)
        except Exception:
            self.stop_event.wait(delay / 1000.0)

    def _wait_for_manual_login(self, page) -> bool:
        """Pause without consuming the visitor-selection timeout until login finishes."""

        self.log(
            "[CGV] 로그인 세션이 만료되었습니다. 열린 Chrome에서 CGV 로그인을 완료해주세요. "
            "로그인 확인 후 예약을 자동으로 계속합니다.",
            "warning",
        )
        login_assistant = CgvLoginAssistant(self.log, self.stop_event)
        while not self.stop_event.is_set():
            try:
                if hasattr(page, "is_closed") and page.is_closed():
                    self.log("[CGV] 로그인 대기 중 Chrome 탭이 닫혔습니다.", "warning")
                    return False
            except Exception:
                pass

            if not self._login_required(page):
                self.log("[CGV] 로그인 완료를 확인했습니다 · 예약 흐름을 자동으로 재개합니다.", "success")
                return True
            login_assistant.step(page)
            self._visitor_wait(page, self.LOGIN_LOCAL_POLL_MS)
        return False

    @staticmethod
    def _restore_visitor_query(page, payload: dict[str, Any]) -> bool:
        try:
            return bool(
                page.evaluate(
                    r"""
                    payload => {
                      try {
                        sessionStorage.setItem('query', JSON.stringify(payload || {}));
                        sessionStorage.setItem('rsrtHistoryBack', 'Y');
                        return true;
                      } catch (_) {
                        return false;
                      }
                    }
                    """,
                    payload,
                )
            )
        except Exception:
            return False

    @staticmethod
    def _visitor_ui_snapshot(page, people: int) -> dict[str, Any]:
        """Inspect visitor controls without relying on one brittle DOM path."""

        try:
            result = page.evaluate(
                r"""
                people => {
                  const clean = value => String(value || '').replace(/\s+/g, '');
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const selected = node => {
                    if (!node) return false;
                    const classes = String(node.className || '').toLowerCase();
                    const tokens = new Set(classes.split(/[\s_\-]+/));
                    return node.title === '선택됨' ||
                           node.getAttribute('aria-pressed') === 'true' ||
                           node.getAttribute('aria-selected') === 'true' ||
                           node.getAttribute('aria-checked') === 'true' ||
                           node.getAttribute('data-selected') === 'true' ||
                           tokens.has('selected') || tokens.has('active') || tokens.has('on');
                  };

                  const path = String(location.pathname || '');
                  const seatButtons = Array.from(
                    document.querySelectorAll('button[data-seatlocno]')
                  ).filter(visible);
                  const controls = Array.from(
                    document.querySelectorAll('button, a, div[role="button"]')
                  ).filter(visible);
                  const hasControl = label => controls.some(node => clean(node.textContent) === label);
                  const modalOpen = seatButtons.length > 0 ||
                                    hasControl('인원변경') || hasControl('선택완료');

                  const leaves = Array.from(document.querySelectorAll('body *')).filter(node =>
                    visible(node) && node.children.length === 0 && clean(node.textContent) === '일반'
                  );
                  let generalFound = leaves.length > 0;
                  let target = null;
                  for (const label of leaves) {
                    let box = label;
                    for (let depth = 0; depth < 9 && box; depth += 1, box = box.parentElement) {
                      const candidate = Array.from(box.querySelectorAll('button')).find(button =>
                        visible(button) && clean(button.textContent) === String(people)
                      );
                      if (candidate) {
                        target = candidate;
                        break;
                      }
                    }
                    if (target) break;
                  }

                  const selectButton = controls.find(node => clean(node.textContent) === '선택');
                  return {
                    path,
                    routeReady: path.includes('/cnm/selectVisitorCnt'),
                    generalFound,
                    targetFound: Boolean(target),
                    targetSelected: selected(target),
                    targetEnabled: Boolean(target && !target.disabled &&
                      target.getAttribute('aria-disabled') !== 'true'),
                    targetClass: target ? String(target.className || '') : '',
                    targetTitle: target ? String(target.title || '') : '',
                    selectFound: Boolean(selectButton),
                    selectEnabled: Boolean(selectButton && !selectButton.disabled &&
                      selectButton.getAttribute('aria-disabled') !== 'true'),
                    modalOpen,
                    seatCount: seatButtons.length,
                  };
                }
                """,
                max(1, int(people)),
            )
            return dict(result) if isinstance(result, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _click_visitor_count(page, people: int) -> bool:
        try:
            return bool(
                page.evaluate(
                    r"""
                    people => {
                      const clean = value => String(value || '').replace(/\s+/g, '');
                      const visible = node => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                               rect.width > 0 && rect.height > 0;
                      };
                      const leaves = Array.from(document.querySelectorAll('body *')).filter(node =>
                        visible(node) && node.children.length === 0 && clean(node.textContent) === '일반'
                      );
                      for (const label of leaves) {
                        let box = label;
                        for (let depth = 0; depth < 9 && box; depth += 1, box = box.parentElement) {
                          const target = Array.from(box.querySelectorAll('button')).find(button =>
                            visible(button) && clean(button.textContent) === String(people)
                          );
                          if (!target) continue;
                          if (target.disabled || target.getAttribute('aria-disabled') === 'true') {
                            return false;
                          }
                          target.scrollIntoView({block: 'center', inline: 'center'});
                          target.click();
                          return true;
                        }
                      }
                      return false;
                    }
                    """,
                    max(1, int(people)),
                )
            )
        except Exception:
            return False

    @staticmethod
    def _click_visitor_submit(page) -> bool:
        try:
            return bool(
                page.evaluate(
                    r"""
                    () => {
                      const clean = value => String(value || '').replace(/\s+/g, '');
                      const visible = node => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                               rect.width > 0 && rect.height > 0;
                      };
                      const target = Array.from(
                        document.querySelectorAll('button, a, div[role="button"]')
                      ).find(node => visible(node) && clean(node.textContent) === '선택');
                      if (!target || target.disabled || target.getAttribute('aria-disabled') === 'true') {
                        return false;
                      }
                      target.scrollIntoView({block: 'center', inline: 'center'});
                      target.click();
                      return true;
                    }
                    """
                )
            )
        except Exception:
            return False

    def _open_visitor_route(self, page, payload: dict[str, Any]) -> bool:
        """Restore screening state, wait for React hydration, and resume after login."""

        for _attempt in range(3):
            if self.stop_event.is_set():
                return False

            if self._login_required(page):
                if not self._wait_for_manual_login(page):
                    return False

            self._restore_visitor_query(page, payload)
            try:
                if "/cnm/selectVisitorCnt" not in str(page.url or ""):
                    page.goto(
                        f"{CGV_HOME_URL}/cnm/selectVisitorCnt",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
            except Exception as exc:
                self.log(f"[CGV] 관람인원 페이지 이동 실패: {exc.__class__.__name__}", "warning")
                continue

            if self._login_required(page):
                if not self._wait_for_manual_login(page):
                    return False
                # Login redirects can replace the page state. Restore the exact
                # screening query and explicitly return to the visitor route.
                self._restore_visitor_query(page, payload)
                try:
                    page.goto(
                        f"{CGV_HOME_URL}/cnm/selectVisitorCnt",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception:
                    continue

            hydration_deadline = time.monotonic() + self.VISITOR_HYDRATION_TIMEOUT_SECONDS
            while time.monotonic() < hydration_deadline and not self.stop_event.is_set():
                if self._login_required(page):
                    break
                if self._is_block_page(page):
                    self.log("[CGV] 관람인원 페이지에서 CGV 접근 제한이 감지되었습니다.", "warning")
                    return False
                snapshot = self._visitor_ui_snapshot(page, 1)
                if snapshot.get("modalOpen") or snapshot.get("generalFound"):
                    return True
                self._visitor_wait(page)

        if self._login_required(page):
            # Give manual login one final unbounded chance instead of ending the
            # booking with a misleading visitor-selection error.
            if self._wait_for_manual_login(page):
                return self._open_visitor_route(page, payload)
            return False

        self.log(
            "[CGV] 관람인원 페이지는 열렸지만 '일반' 인원 선택 UI의 React 준비를 확인하지 못했습니다.",
            "error",
        )
        return False

    def _enter_visitor_page(self, page, schedule: dict[str, Any]) -> bool:
        payload = self._query_payload(schedule)
        self._visitor_query_payload = dict(payload)
        return self._open_visitor_route(page, self._visitor_query_payload)

    def _select_visitors(self, page, people: int) -> bool:
        """Select the exact visitor count, verify the state, then open seat data."""

        target_num = max(1, int(people))
        deadline = time.monotonic() + self.VISITOR_SELECTION_TIMEOUT
        last_snapshot: dict[str, Any] = {}
        count_clicked = False
        count_clicked_at = 0.0
        submit_clicks = 0
        submit_clicked_at = 0.0
        modal_open_at = 0.0
        count_verified_logged = False

        while not self.stop_event.is_set():
            if self._login_required(page):
                if not self._wait_for_manual_login(page):
                    return False
                if not self._open_visitor_route(page, self._visitor_query_payload):
                    return False
                # Manual login time must not consume the visitor-selection
                # budget. Start a fresh short budget after the route is restored.
                deadline = time.monotonic() + self.VISITOR_SELECTION_TIMEOUT
                count_clicked = False
                count_clicked_at = 0.0
                submit_clicks = 0
                submit_clicked_at = 0.0
                modal_open_at = 0.0
                continue

            now = time.monotonic()
            if now >= deadline:
                break

            last_snapshot = self._visitor_ui_snapshot(page, target_num)
            if not last_snapshot:
                self._visitor_wait(page)
                continue

            seat_count = int(last_snapshot.get("seatCount", 0) or 0)
            if seat_count > 0:
                self.log(
                    f"[CGV] 관람인원 {target_num}명 확인 · 좌석 모달 로드 완료",
                    "info",
                )
                return True

            if last_snapshot.get("modalOpen"):
                if not modal_open_at:
                    modal_open_at = now
                if now - modal_open_at >= self.VISITOR_SEAT_DATA_TIMEOUT_SECONDS:
                    self.log(
                        "[CGV] 좌석 모달은 열렸지만 실제 좌석 데이터가 제한 시간 안에 로드되지 않았습니다.",
                        "warning",
                    )
                    return False
                self._visitor_wait(page)
                continue

            if not last_snapshot.get("routeReady"):
                if not self._open_visitor_route(page, self._visitor_query_payload):
                    return False
                deadline = time.monotonic() + self.VISITOR_SELECTION_TIMEOUT
                continue

            if not last_snapshot.get("generalFound"):
                self._visitor_wait(page)
                continue

            if not last_snapshot.get("targetFound"):
                self.log(
                    f"[CGV] '일반' 관람인원 {target_num}명 버튼을 찾지 못했습니다.",
                    "error",
                )
                return False

            target_selected = bool(last_snapshot.get("targetSelected"))
            if target_selected and not count_verified_logged:
                count_verified_logged = True
                self.log(f"[CGV] 관람인원 일반 {target_num}명 선택 상태 확인", "info")

            if not count_clicked and not target_selected:
                if not last_snapshot.get("targetEnabled"):
                    self.log(
                        f"[CGV] 관람인원 일반 {target_num}명 버튼이 비활성화되어 있습니다.",
                        "error",
                    )
                    return False
                if self._click_visitor_count(page, target_num):
                    count_clicked = True
                    count_clicked_at = now
                    self._visitor_wait(page)
                    continue
                self._visitor_wait(page)
                continue

            # Prefer an explicit selected marker. Some CGV builds only expose the
            # state indirectly by enabling the final '선택' action; after one
            # successful count click, that enabled action is a safe secondary
            # signal and the modal opening remains the final verification.
            count_applied = target_selected
            if count_clicked and last_snapshot.get("selectEnabled"):
                count_applied = True
            if count_clicked and not count_applied:
                if now - count_clicked_at < self.VISITOR_COUNT_VERIFY_SECONDS:
                    self._visitor_wait(page)
                    continue
                self.log(
                    f"[CGV] 일반 {target_num}명 버튼은 클릭했지만 선택 상태 반영을 확인하지 못했습니다.",
                    "error",
                )
                return False

            if not count_applied:
                self._visitor_wait(page)
                continue

            if not last_snapshot.get("selectFound"):
                self.log("[CGV] 관람인원 확정용 '선택' 버튼을 찾지 못했습니다.", "error")
                return False

            if not last_snapshot.get("selectEnabled"):
                self._visitor_wait(page)
                continue

            if submit_clicks == 0 or (
                submit_clicks < 2
                and submit_clicked_at
                and now - submit_clicked_at >= self.VISITOR_MODAL_OPEN_TIMEOUT_SECONDS / 2
            ):
                if self._click_visitor_submit(page):
                    submit_clicks += 1
                    submit_clicked_at = now
                    self._visitor_wait(page)
                    continue

            if submit_clicked_at and now - submit_clicked_at >= self.VISITOR_MODAL_OPEN_TIMEOUT_SECONDS:
                self.log(
                    "[CGV] 관람인원 선택은 확인했지만 '선택' 클릭 후 좌석 모달이 열리지 않았습니다.",
                    "error",
                )
                return False

            self._visitor_wait(page)

        if self._login_required(page):
            # A redirect at the exact timeout boundary should still be presented
            # to the user as a login requirement, not a visitor-selection error.
            if self._wait_for_manual_login(page):
                if self._open_visitor_route(page, self._visitor_query_payload):
                    return self._select_visitors(page, target_num)
            return False

        if not last_snapshot.get("generalFound"):
            self.log("[CGV] 관람인원 페이지에서 '일반' 선택 영역을 찾지 못했습니다.", "error")
        elif not last_snapshot.get("targetFound"):
            self.log(f"[CGV] 일반 {target_num}명 선택 버튼을 찾지 못했습니다.", "error")
        elif count_clicked and not last_snapshot.get("targetSelected"):
            self.log(
                f"[CGV] 일반 {target_num}명 클릭 후 선택 상태 검증이 시간 안에 끝나지 않았습니다.",
                "error",
            )
        elif submit_clicks:
            self.log("[CGV] 관람인원 확정 후 좌석 모달 전환이 시간 안에 완료되지 않았습니다.", "error")
        else:
            self.log("[CGV] 관람인원 선택 단계를 완료하지 못했습니다.", "error")
        return False
