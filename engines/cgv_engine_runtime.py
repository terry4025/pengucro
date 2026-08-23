from __future__ import annotations

import base64
from contextvars import ContextVar
import json
import math
import time
from typing import Any, Mapping

import engines.cgv_engine as _base_cgv_engine
from engines.cgv_chrome_session import CgvBrowserSessionProxy
from engines.cgv_engine_guarded import CgvEngine as GuardedCgvEngine
from engines.cgv_client import CGV_COMPANY_CODE, CGV_HOME_URL


_MEMBER_SESSION_GUARD_ACTIVE: ContextVar[bool] = ContextVar(
    "pengucro_cgv_member_session_guard_active",
    default=True,
)


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
    * the visitor/seat hand-off can use CGV's already-captured first seat API
      response instead of waiting for the whole seat DOM to finish painting;
    * a successful API hold gets a longer React/UI synchronization grace period
      before the base engine considers releasing it and using browser fallback;
    * CGV's intermediate checkout confirmation is acknowledged;
    * an existing member session is reused;
    * every CGV operation stays on persistent Chrome slot 1 / port 9333;
    * when several seat priorities are configured, the group that actually wins
      becomes authoritative and stale selections from other groups are cleared
      before ``선택완료`` is submitted.
    """

    # Keep pre-open traffic bounded, but remove most of the old 2-second blind
    # window. A partial publication is more valuable, so it gets the tighter
    # cadence while the existing 403/429 policy can still slow requests down.
    PREOPEN_IDLE_INTERVAL = 0.75
    SCHEDULE_HINT_INTERVAL = 0.5

    # The base visitor loop sleeps 350 ms between state checks. Once the official
    # page has accepted the visitor count, polling DOM readiness is local work;
    # checking it more frequently avoids adding hundreds of milliseconds after
    # the actual target screening appears. Corrective clicks remain rate-limited
    # separately so this does not repeatedly submit the visitor form.
    VISITOR_READY_POLL_INTERVAL_MS = 60
    VISITOR_ACTION_RETRY_MS = 700

    # A successful direct hold is more valuable than shaving a second from UI
    # rendering. The previous 40 * 25 ms window could release an already-won
    # hold because React had not enabled 선택완료 yet. Give that state up to about
    # three seconds while avoiding repeated seat toggles when the exact group is
    # already selected and only the submit button is lagging.
    API_UI_SYNC_ATTEMPTS = 120

    MEMBER_SESSION_EXPIRY_LEEWAY_SECONDS = 60.0
    MEMBER_SESSION_PROBE_INTERVAL_MS = 60_000
    MEMBER_SESSION_GUARD_READ_INTERVAL_SECONDS = 5.0
    MEMBER_SESSION_PROBE_URL = (
        f"{CGV_HOME_URL}/api/v1/mypage/tkt/mblTkt/"
        f"searchMblTktTabPrdtypList?coCd={CGV_COMPANY_CODE}&custNo="
    )
    MEMBER_SESSION_AUTH_ERROR_CODES = {"-1001", "-1002", "401"}

    @staticmethod
    def _context_member_tokens(context) -> dict[str, str]:
        tokens = {"accessToken": "", "refresh_token": ""}
        try:
            for cookie in context.cookies(CGV_HOME_URL):
                name = str(cookie.get("name", "") or "")
                if name in tokens:
                    tokens[name] = str(cookie.get("value", "") or "").strip()
        except Exception:
            pass
        return tokens

    @staticmethod
    def _context_has_member_session(context) -> bool:
        return any(CgvEngine._context_member_tokens(context).values())

    @staticmethod
    def _jwt_expiry(access_token: str) -> float | None:
        parts = str(access_token or "").strip().split(".")
        if len(parts) != 3 or not parts[1]:
            return None
        try:
            encoded = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
            expiry = payload.get("exp") if isinstance(payload, Mapping) else None
            if isinstance(expiry, bool):
                return None
            value = float(expiry)
            return value if math.isfinite(value) else None
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    @classmethod
    def _jwt_is_fresh(cls, access_token: str) -> bool | None:
        expiry = cls._jwt_expiry(access_token)
        if expiry is None:
            return None
        return expiry > time.time() + cls.MEMBER_SESSION_EXPIRY_LEEWAY_SECONDS

    @classmethod
    def _member_probe_auth_error(cls, result: Mapping[str, Any]) -> bool:
        try:
            if int(result.get("status", 0) or 0) == 401:
                return True
        except (TypeError, ValueError):
            pass
        payload = result.get("data")
        if not isinstance(payload, Mapping):
            return False
        codes = [payload.get("statusCode")]
        nested = payload.get("data")
        if isinstance(nested, Mapping):
            codes.append(nested.get("statusCode"))
        return any(str(code).strip() in cls.MEMBER_SESSION_AUTH_ERROR_CODES for code in codes)

    @classmethod
    def _probe_member_session(cls, page, access_token: str) -> bool | None:
        try:
            result = page.evaluate(
                r"""
                async ({url, accessToken, timeoutMs}) => {
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort(), timeoutMs);
                  try {
                    const headers = new Headers({
                      'Accept': 'application/json',
                      'Accept-Language': 'ko-KR',
                    });
                    if (accessToken) {
                      headers.set('Authorization', `Bearer ${accessToken}`);
                    }
                    const response = await fetch(url, {
                      method: 'GET', cache: 'no-store', credentials: 'include',
                      headers, signal: controller.signal,
                    });
                    let data = null;
                    try { data = await response.json(); } catch (_) {}
                    return {ok: response.ok, status: response.status, data};
                  } catch (error) {
                    return {ok: false, status: 0, data: null, error: String(error)};
                  } finally {
                    clearTimeout(timer);
                  }
                }
                """,
                {
                    "url": cls.MEMBER_SESSION_PROBE_URL,
                    "accessToken": str(access_token or ""),
                    "timeoutMs": 3000,
                },
            )
        except Exception:
            return None
        if not isinstance(result, Mapping):
            return None
        if cls._member_probe_auth_error(result):
            return False
        try:
            status = int(result.get("status", 0) or 0)
        except (TypeError, ValueError):
            return None
        # This read-only endpoint authenticates before validating custNo. A
        # valid session therefore returns either 200 or the expected 400 input
        # error when the intentionally blank custNo reaches domain validation.
        if status in {200, 400} and isinstance(result.get("data"), Mapping):
            return True
        return None

    @classmethod
    def _install_member_session_guard(cls, page) -> dict[str, Any]:
        try:
            result = page.evaluate(
                r"""
                ({url, intervalMs, authCodes}) => {
                  if (window.__pengucroMemberSessionProbe) {
                    return {...window.__pengucroMemberSessionProbe};
                  }
                  const state = window.__pengucroMemberSessionProbe = {
                    unauthorized: false, checkedAt: 0, inFlight: false,
                  };
                  const run = async () => {
                    if (state.inFlight || state.unauthorized) return;
                    state.inFlight = true;
                    try {
                      const item = String(document.cookie || '').split('; ')
                        .find(value => value.startsWith('accessToken='));
                      let token = item ? item.slice('accessToken='.length) : '';
                      try { token = decodeURIComponent(token); } catch (_) {}
                      const headers = new Headers({
                        'Accept': 'application/json',
                        'Accept-Language': 'ko-KR',
                      });
                      if (token) headers.set('Authorization', `Bearer ${token}`);
                      const response = await fetch(url, {
                        method: 'GET', cache: 'no-store', credentials: 'include', headers,
                      });
                      let data = null;
                      try { data = await response.json(); } catch (_) {}
                      const codes = [data && data.statusCode,
                        data && data.data && data.data.statusCode]
                        .map(value => String(value ?? '').trim());
                      if (response.status === 401 || codes.some(code => authCodes.includes(code))) {
                        state.unauthorized = true;
                      }
                      state.checkedAt = Date.now();
                    } catch (_) {
                      // Network/WAF failures are handled by the existing schedule
                      // backoff and must not be mistaken for an expired login.
                    } finally {
                      state.inFlight = false;
                    }
                  };
                  run();
                  state.timer = setInterval(run, intervalMs);
                  return {...state, timer: Boolean(state.timer)};
                }
                """,
                {
                    "url": cls.MEMBER_SESSION_PROBE_URL,
                    "intervalMs": cls.MEMBER_SESSION_PROBE_INTERVAL_MS,
                    "authCodes": sorted(cls.MEMBER_SESSION_AUTH_ERROR_CODES),
                },
            )
            return dict(result) if isinstance(result, Mapping) else {}
        except Exception:
            return {}

    def _recover_member_session(self, page, context) -> bool:
        recovered = super()._ensure_member_session(page, context)
        if recovered:
            self._install_member_session_guard(page)
        return recovered

    def _ensure_member_session(self, page, context) -> bool:
        if not _MEMBER_SESSION_GUARD_ACTIVE.get():
            return True
        tokens = self._context_member_tokens(context)
        access_token = tokens["accessToken"]
        freshness = self._jwt_is_fresh(access_token) if access_token else False
        if freshness is False:
            self.log(
                "[CGV] 저장된 회원 accessToken이 만료됐거나 없어 공식 로그인/갱신 경로로 전환합니다.",
                "warning",
            )
            return self._recover_member_session(page, context)

        if freshness is True or self._probe_member_session(page, access_token) is True:
            self.log(
                "[CGV] 슬롯 1의 기존 Chrome 로그인 세션 확인 · 현재 CGV 탭을 그대로 재사용합니다.",
                "success",
            )
            self._install_member_session_guard(page)
            return True

        self.log(
            "[CGV] 저장된 회원 세션의 인증 만료/무효 가능성을 감지해 공식 로그인/갱신 경로로 전환합니다.",
            "warning",
        )
        return self._recover_member_session(page, context)

    def _race_schedule(self, page, url: str, concurrency: int) -> dict[str, Any]:
        if not _MEMBER_SESSION_GUARD_ACTIVE.get():
            return super()._race_schedule(page, url, concurrency)
        now = time.monotonic()
        try:
            page_url = str(getattr(page, "url", "") or "")
        except Exception:
            page_url = ""
        page_signature = (id(page), page_url)
        previous_signature = getattr(self, "_member_guard_page_signature", None)
        last_read = float(getattr(self, "_member_guard_last_read", 0.0) or 0.0)
        should_read = (
            page_signature != previous_signature
            or now - last_read >= self.MEMBER_SESSION_GUARD_READ_INTERVAL_SECONDS
        )
        if should_read:
            self._member_guard_page_signature = page_signature
            self._member_guard_last_read = now
            state = self._install_member_session_guard(page)
            if state.get("unauthorized"):
                context = getattr(page, "context", None) or getattr(self, "_context", None)
                self.log(
                    "[CGV] 감시 중 회원 세션 만료를 감지했습니다. 로그인/토큰 갱신 후 감시를 계속합니다.",
                    "warning",
                )
                if context is None or not self._recover_member_session(page, context):
                    return {
                        "ok": False,
                        "status": 401,
                        "statuses": [401],
                        "error": "member-session-expired",
                        "elapsedMs": 0.0,
                    }
                self._member_guard_page_signature = None
        return super()._race_schedule(page, url, concurrency)

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
        member_booking = str(cgv.get("booking_mode", "회원") or "회원").strip() != "비회원"
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
        member_guard_token = _MEMBER_SESSION_GUARD_ACTIVE.set(member_booking)
        try:
            return super().make_reservation_thread(data)
        finally:
            _MEMBER_SESSION_GUARD_ACTIVE.reset(member_guard_token)

    def _captured_initial_seat_ready(self) -> bool:
        captured = getattr(self, "_initial_seat_response", None)
        if not isinstance(captured, dict):
            return False
        status = int(captured.get("status", 0) or 0)
        return 200 <= status < 300 and isinstance(captured.get("data"), dict)

    @staticmethod
    def _click_visitor_count(page, people: int) -> bool:
        try:
            return bool(
                page.evaluate(
                    r"""
                    people => {
                      const clean = value => (value || '').replace(/\s+/g, '');
                      const nodes = Array.from(document.querySelectorAll('*'));
                      const labels = nodes.filter(node =>
                        node.children.length === 0 && clean(node.textContent) === '일반'
                      );
                      for (const label of labels) {
                        let box = label;
                        for (let depth = 0; box && depth < 7; depth += 1, box = box.parentElement) {
                          const target = Array.from(box.querySelectorAll('button')).find(button =>
                            !button.disabled && button.getAttribute('aria-disabled') !== 'true' &&
                            clean(button.textContent) === String(people)
                          );
                          if (target) {
                            target.click();
                            return true;
                          }
                        }
                      }
                      return false;
                    }
                    """,
                    max(1, min(int(people), 8)),
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
                      const clean = value => (value || '').replace(/\s+/g, '');
                      const visible = node => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                               rect.width > 0 && rect.height > 0;
                      };
                      const target = Array.from(
                        document.querySelectorAll('button, a, div[role="button"]')
                      ).find(node =>
                        visible(node) && !node.disabled &&
                        node.getAttribute('aria-disabled') !== 'true' &&
                        clean(node.textContent) === '선택'
                      );
                      if (!target) return false;
                      target.click();
                      return true;
                    }
                    """
                )
            )
        except Exception:
            return False

    def _select_visitors(self, page, people: int) -> bool:
        """Open the seat modal without adding avoidable post-detection latency.

        Visitor controls are only retried after a bounded recovery interval.
        Once CGV's own first seat response has arrived, the fast monitor can use
        that response immediately even if React is still painting seat buttons.
        """

        target = self._current_page(page)
        if target is None:
            return False
        self._sync_runtime_handles_from_page(target)

        start_time = time.monotonic()
        target_num = max(1, min(int(people), 8))
        visitor_chosen = False
        last_people_attempt = -1.0
        submit_clicked_at = -1.0
        last_snapshot: dict = {}

        while (
            not self.stop_event.is_set()
            and time.monotonic() - start_time < self.VISITOR_SELECTION_TIMEOUT
        ):
            last_snapshot = self._seat_modal_snapshot(target)
            if int(last_snapshot.get("seatCount", 0) or 0) > 0:
                self._sync_runtime_handles_from_page(target)
                return True

            if last_snapshot.get("modalOpen"):
                if self._captured_initial_seat_ready():
                    self.log(
                        "[CGV] 최초 좌석 API 응답 수신 · 좌석 DOM 렌더 완료를 기다리지 않고 고속 선점 준비를 계속합니다.",
                        "info",
                    )
                    self._sync_runtime_handles_from_page(target)
                    return True
            else:
                now = time.monotonic()
                if (
                    not visitor_chosen
                    and (
                        last_people_attempt < 0
                        or now - last_people_attempt
                        >= self.VISITOR_ACTION_RETRY_MS / 1000.0
                    )
                ):
                    last_people_attempt = now
                    visitor_chosen = self._click_visitor_count(target, target_num)

                # Give React at least one local readiness tick after visitor
                # selection. If the submit button is not ready yet, retry this
                # local DOM action on the next 60 ms tick without re-clicking the
                # visitor count and without issuing another seat API request.
                if visitor_chosen and submit_clicked_at < 0:
                    if self._click_visitor_submit(target):
                        submit_clicked_at = now

                # If CGV never opened the modal, allow one clean corrective
                # visitor-selection cycle rather than hammering both controls.
                if (
                    submit_clicked_at >= 0
                    and now - submit_clicked_at
                    >= self.VISITOR_ACTION_RETRY_MS / 1000.0
                ):
                    visitor_chosen = False
                    submit_clicked_at = -1.0

            try:
                target.wait_for_timeout(self.VISITOR_READY_POLL_INTERVAL_MS)
            except Exception:
                if self.stop_event.wait(self.VISITOR_READY_POLL_INTERVAL_MS / 1000.0):
                    break

        last_snapshot = self._seat_modal_snapshot(target) or last_snapshot
        if int(last_snapshot.get("seatCount", 0) or 0) > 0:
            self._sync_runtime_handles_from_page(target)
            return True
        if last_snapshot.get("modalOpen"):
            self.log(
                "CGV 좌석 모달은 열렸지만 좌석 데이터가 제한 시간 안에 로드되지 않았습니다.",
                "warning",
            )
        else:
            self.log("CGV 관람 인원 선택 및 좌석 모달 열기에 실패했습니다.", "error")
        return False

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
        last_snapshot: dict = {}
        for attempt in range(self.API_UI_SYNC_ATTEMPTS):
            snapshot = self._exact_seat_selection_snapshot(page, target_ids)
            if snapshot:
                last_snapshot = snapshot
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

            missing = list(snapshot.get("missing") or []) if snapshot else []
            extras = list(snapshot.get("extras") or []) if snapshot else []
            exact_group_selected = bool(snapshot) and not missing and not extras

            # If the exact held group is already selected, do not keep toggling
            # it simply because React has not enabled 선택완료 yet. Otherwise make
            # bounded corrective attempts, but a temporary missing/unavailable
            # DOM node no longer releases a successful hold immediately.
            should_apply = (
                not exact_group_selected
                and (attempt == 0 or attempt % 4 == 0)
            )
            if should_apply:
                self._apply_exact_seat_selection(page, target_ids)

            try:
                page.wait_for_timeout(self.API_UI_SYNC_INTERVAL_MS)
            except Exception:
                time.sleep(self.API_UI_SYNC_INTERVAL_MS / 1000.0)

        # Preserve legacy mocked-page compatibility when no DOM snapshot can be
        # observed; the submit helper still checks the enabled button itself.
        if not observed_snapshot:
            return True

        selected = list(last_snapshot.get("selectedIds") or [])
        self.log(
            "[CGV] 임시선점은 유지했지만 좌석 화면 동기화가 약 "
            f"{self.API_UI_SYNC_ATTEMPTS * self.API_UI_SYNC_INTERVAL_MS / 1000.0:.1f}초 내 "
            f"완료되지 않았습니다 · 선택 상태 {len(selected)}/{len(target_ids)}석",
            "warning",
        )
        return False

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
