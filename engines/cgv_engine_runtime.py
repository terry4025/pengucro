from __future__ import annotations

import base64
from contextvars import ContextVar
import json
import math
import time
from typing import Any, Mapping
from urllib.parse import quote, unquote

import engines.cgv_engine as _base_cgv_engine
from engines.cgv_chrome_session import CgvBrowserSessionProxy
from engines.cgv_engine_guarded import CgvEngine as GuardedCgvEngine
from engines.cgv_client import CGV_COMPANY_CODE, CGV_HOME_URL
from engines.naver_api import ACCOUNT_QUERY


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
    # hold because React had not enabled 선택완료 yet. Give each local recovery
    # pass up to about six seconds and retain the already-won API hold across a
    # second bounded pass. This adds no CGV polling traffic.
    API_UI_SYNC_ATTEMPTS = 240
    API_HOLD_UI_SYNC_MAX_ATTEMPTS = 2

    # Successful seat selection is already protected by CGV's temporary hold.
    # Congested checkout/Naver Pay pages should not be classified as failed just
    # because they need more than the old 15-20 second UI grace. These are local
    # observation deadlines and do not add duplicate payment or hold requests.
    SEAT_SUBMIT_TRANSITION_TIMEOUT_SECONDS = 20.0
    CGV_PAYMENT_PAGE_TIMEOUT_SECONDS = 30.0
    NPAY_PAGE_TIMEOUT_SECONDS = 45.0
    NPAY_CONTROL_TIMEOUT_SECONDS = 30.0
    NPAY_COMPLETION_TIMEOUT_SECONDS = 120.0

    MEMBER_SESSION_EXPIRY_LEEWAY_SECONDS = 60.0
    MEMBER_SESSION_PROBE_INTERVAL_MS = 60_000
    MEMBER_SESSION_GUARD_READ_INTERVAL_SECONDS = 5.0
    MEMBER_SESSION_PROBE_TIMEOUT_SECONDS = 5.0
    MEMBER_SESSION_PROOF_MAX_AGE_SECONDS = 90.0
    MEMBER_SESSION_PROBE_URL = (
        f"{CGV_HOME_URL}/api/v1/mypage/tkt/mblTkt/"
        f"searchMblTktTabPrdtypList?coCd={CGV_COMPANY_CODE}&custNo="
    )
    MEMBER_SESSION_AUTH_ERROR_CODES = {"-1001", "-1002", "401"}
    MEMBER_SESSION_CONFIRM_ATTEMPTS = 3
    MEMBER_SESSION_CONFIRM_INTERVAL_MS = 250
    NAVER_ACCOUNT_PROBE_URL = "https://m.booking.naver.com/graphql?opName=account"
    NAVER_ACCOUNT_HOME_URL = "https://m.booking.naver.com/"
    NAVER_ACCOUNT_PROBE_TIMEOUT_MS = 5_000

    @staticmethod
    def _context_member_tokens(context) -> dict[str, str]:
        tokens = {"accessToken": "", "refresh_token": ""}
        try:
            for cookie in context.cookies(CGV_HOME_URL):
                name = str(cookie.get("name", "") or "")
                if name in tokens:
                    # CGV stores accessToken URL-encoded. Browser-side booking
                    # requests decode it before constructing the Bearer header;
                    # the startup member probe must use the exact same form.
                    tokens[name] = unquote(
                        str(cookie.get("value", "") or "").strip()
                    )
        except Exception:
            pass
        return tokens

    @classmethod
    def _context_naver_login_state(cls, context) -> bool | None:
        """Read the live Naver account state using slot 1's shared cookie jar."""

        try:
            response = context.request.post(
                cls.NAVER_ACCOUNT_PROBE_URL,
                data={
                    "operationName": "account",
                    "query": ACCOUNT_QUERY,
                    "variables": {},
                },
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://m.booking.naver.com",
                    "Referer": cls.NAVER_ACCOUNT_HOME_URL,
                },
                timeout=cls.NAVER_ACCOUNT_PROBE_TIMEOUT_MS,
            )
            body = response.json()
        except Exception:
            return None
        if int(getattr(response, "status", 0) or 0) != 200:
            return None
        data = body.get("data") if isinstance(body, Mapping) else None
        account = data.get("account") if isinstance(data, Mapping) else None
        if not isinstance(account, Mapping):
            return None
        return bool(account.get("isLoggedIn"))

    def _ensure_naver_session_before_booking(self, page, context) -> bool:
        """Fail early on a logged-out Naver profile, before a CGV seat is held."""

        state = self._context_naver_login_state(context)
        if state is True:
            self.log(
                "[CGV] 슬롯 1의 영구 Chrome 프로필에서 네이버 로그인 세션 확인 완료",
                "success",
            )
            return True
        if state is None:
            self.log(
                "[CGV] 네이버 로그인 API 확인이 일시적으로 지연됐습니다. "
                "영구 프로필은 유지하고 N pay 전환 시 다시 확인합니다.",
                "warning",
            )
            return True

        login_page = None
        created_page = False
        try:
            login_page = next(
                (
                    candidate
                    for candidate in context.pages
                    if not candidate.is_closed()
                    and self._is_naver_login_url(self._safe_page_url(candidate))
                ),
                None,
            )
        except Exception:
            login_page = None
        if login_page is None:
            try:
                login_page = context.new_page()
                created_page = True
                return_url = quote(
                    self.NAVER_ACCOUNT_HOME_URL,
                    safe="",
                )
                login_page.goto(
                    f"https://nid.naver.com/nidlogin.login?url={return_url}",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except Exception:
                login_page = None
        if login_page is None:
            self.log(
                "[CGV] 네이버 로그인 세션이 없고 로그인 화면도 준비하지 못했습니다.",
                "error",
            )
            return False

        self.log(
            "[CGV] 네이버 로그인 세션이 없어 예약 감시 전에 자동 로그인 복구를 시작합니다.",
            "warning",
        )
        login_clicked = False
        credentials_reported = False
        verification_reported = False
        deadline = time.monotonic() + self.NPAY_COMPLETION_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline and not self.stop_event.is_set():
                state = self._context_naver_login_state(context)
                if state is True:
                    self.log(
                        "[CGV] 네이버 로그인 복구 완료 · 영구 Chrome 프로필에 세션 유지",
                        "success",
                    )
                    try:
                        page.bring_to_front()
                    except Exception:
                        pass
                    return True

                login_state = self._click_prefilled_naver_login(
                    login_page,
                    allow_click=not login_clicked,
                )
                if login_state.get("clicked"):
                    login_clicked = True
                    self.log(
                        "[CGV] 저장된 네이버 입력으로 로그인 버튼 클릭 완료",
                        "info",
                    )
                elif login_state.get("found") and not login_state.get("filled"):
                    if not credentials_reported:
                        credentials_reported = True
                        self.log(
                            "[CGV] 슬롯 1에 저장된 네이버 입력이 없습니다. "
                            "열린 Chrome에서 로그인하면 예약 감시를 자동으로 시작합니다.",
                            "warning",
                        )
                elif (
                    self._naver_additional_verification_visible(login_page)
                    and not verification_reported
                ):
                    verification_reported = True
                    self.log(
                        "[CGV] 네이버 추가 보안 확인이 필요합니다. "
                        "열린 Chrome에서 완료하면 예약 감시를 자동으로 시작합니다.",
                        "warning",
                    )
                try:
                    login_page.wait_for_timeout(500)
                except Exception:
                    if self.stop_event.wait(0.5):
                        break
        finally:
            if created_page and login_page is not None:
                try:
                    if not login_page.is_closed():
                        login_page.close()
                except Exception:
                    pass

        self.log(
            "[CGV] 네이버 로그인 복구를 확인하지 못해 결제 불가능한 상태의 예약 시작을 차단했습니다.",
            "error",
        )
        return False

    def _prepare_authentication(self, page, context, cgv: dict[str, Any]) -> bool:
        if not super()._prepare_authentication(page, context, cgv):
            return False
        if str(cgv.get("booking_mode", "회원") or "회원").strip() == "비회원":
            return True
        return self._ensure_naver_session_before_booking(page, context)

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
    def _install_member_session_guard(cls, page, *, start: bool = True) -> dict[str, Any]:
        """Start/read one probe without awaiting fetch or browser timers.

        The host drives the one-minute cadence and cancels a stuck request.
        Late responses belong to their controller and cannot revive a cancelled
        request or overwrite a newly installed probe after login.
        """
        try:
            result = page.evaluate(
                r"""
                ({url, start, authCodes}) => {
                  let state = window.__pengucroMemberSessionProbe;
                  if (state && (state.version !== 2 ||
                      !Number.isInteger(state.requestId) || !Number.isInteger(state.completedId))) {
                    if (state.timer) clearInterval(state.timer);
                    if (state.controller) state.controller.abort();
                    state = null;
                  }
                  if (!state) state = window.__pengucroMemberSessionProbe = {
                    version: 2, unauthorized: false, valid: false,
                    checkedAt: 0, completedId: 0, requestId: 0,
                    inFlight: false, controller: null, startedAt: null, completedAt: null,
                  };
                  const run = async () => {
                    if (state.inFlight || state.unauthorized) return;
                    const controller = new AbortController();
                    state.controller = controller;
                    const requestId = ++state.requestId;
                    state.inFlight = true;
                    state.startedAt = performance.now();
                    const current = () => window.__pengucroMemberSessionProbe === state &&
                      state.controller === controller && !controller.signal.aborted;
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
                        signal: controller.signal,
                      });
                      let data = null;
                      try { data = await response.json(); } catch (_) {}
                      if (!current()) return;
                      const codes = [data && data.statusCode,
                        data && data.data && data.data.statusCode]
                        .map(value => String(value ?? '').trim());
                      if (response.status === 401 || codes.some(code => authCodes.includes(code))) {
                        state.unauthorized = true;
                      }
                      state.valid = !state.unauthorized && [200, 400].includes(response.status) &&
                        data !== null && typeof data === 'object' && !Array.isArray(data);
                      state.checkedAt = Date.now();
                      state.completedAt = performance.now();
                      state.completedId = requestId;
                    } catch (_) {
                      if (current()) {
                        state.valid = false;
                        state.completedAt = performance.now();
                        state.completedId = requestId;
                      }
                    } finally {
                      if (current()) {
                        state.inFlight = false;
                        state.controller = null;
                      }
                    }
                  };
                  if (start) run();
                  return {version: state.version, unauthorized: state.unauthorized,
                    valid: state.valid, checkedAt: state.checkedAt,
                    requestId: state.requestId, completedId: state.completedId,
                    startedAgeMs: state.startedAt === null ? null : performance.now() - state.startedAt,
                    completedAgeMs: state.completedAt === null ? null : performance.now() - state.completedAt,
                    inFlight: state.inFlight};
                }
                """,
                {
                    "url": cls.MEMBER_SESSION_PROBE_URL,
                    "start": bool(start),
                    "authCodes": sorted(cls.MEMBER_SESSION_AUTH_ERROR_CODES),
                },
            )
            return dict(result) if isinstance(result, Mapping) else {}
        except Exception:
            return {}

    @staticmethod
    def _cancel_member_session_probe(page, *, dispose: bool = False) -> None:
        try:
            page.evaluate(r"""dispose => {
              const state = window.__pengucroMemberSessionProbe;
              if (!state) return;
              if (state.timer) clearInterval(state.timer);
              if (state.controller) state.controller.abort();
              state.controller = null;
              state.inFlight = false;
              if (dispose) delete window.__pengucroMemberSessionProbe;
            }""", dispose)
        except Exception:
            pass

    def _mark_member_session_confirmed(self, page) -> None:
        now = time.monotonic()
        self._member_guard_page = page
        self._member_guard_last_proof = now
        self._member_guard_last_start = now
        self._member_guard_page_signature = None
        self._member_guard_error = None
        self._member_guard_retry_after = 0.0

    @staticmethod
    def _clear_invalid_member_tokens(context) -> None:
        """Remove only CGV member tokens after an authoritative auth failure."""

        clear_cookies = getattr(context, "clear_cookies", None)
        if not callable(clear_cookies):
            return
        try:
            cookies = list(context.cookies(CGV_HOME_URL))
        except Exception:
            cookies = []
        for cookie in cookies:
            name = str(cookie.get("name", "") or "")
            if name not in {"accessToken", "refresh_token"}:
                continue
            try:
                clear_cookies(
                    name=name,
                    domain=str(cookie.get("domain", "") or "") or None,
                    path=str(cookie.get("path", "") or "") or None,
                )
            except Exception:
                pass

    def _confirm_member_session(self, page, context) -> bool | None:
        """Require a live read-only member API confirmation before scanning."""

        last_result: bool | None = None
        for attempt in range(max(1, int(self.MEMBER_SESSION_CONFIRM_ATTEMPTS))):
            access_token = self._context_member_tokens(context)["accessToken"]
            last_result = self._probe_member_session(page, access_token)
            if last_result is not None or self.stop_event.is_set():
                return last_result
            if attempt + 1 < self.MEMBER_SESSION_CONFIRM_ATTEMPTS:
                try:
                    page.wait_for_timeout(self.MEMBER_SESSION_CONFIRM_INTERVAL_MS)
                except Exception:
                    if self.stop_event.wait(
                        self.MEMBER_SESSION_CONFIRM_INTERVAL_MS / 1000.0
                    ):
                        break
        return last_result

    def _recover_member_session(self, page, context) -> bool:
        if self.stop_event.is_set():
            return False
        self._cancel_member_session_probe(page, dispose=True)
        recovered = super()._ensure_member_session(page, context)
        if not recovered or self.stop_event.is_set():
            return False

        confirmed = self._confirm_member_session(page, context)
        if confirmed is True:
            self.log(
                "[CGV] 회원 API 인증까지 확인했습니다. 예약 감시를 시작합니다.",
                "success",
            )
            self._install_member_session_guard(page)
            self._mark_member_session_confirmed(page)
            return True

        if confirmed is False:
            self._clear_invalid_member_tokens(context)
            self.log(
                "[CGV] 로그인 화면 전환은 감지했지만 회원 API 인증이 유효하지 않아 "
                "예약 시작을 차단했습니다. 다시 로그인한 뒤 예약 시작을 눌러주세요.",
                "error",
            )
            return False

        # The live probe can time out while CGV is congested. The base recovery
        # just navigated through the official login route and returned only after
        # CGV redirected away with member cookies, so that route transition is a
        # safe secondary proof. Keep the periodic guard active for a later 401.
        self.log(
            "[CGV] 회원 API 응답은 지연됐지만 공식 로그인 화면의 회원 리다이렉트와 "
            "세션 쿠키를 확인했습니다. 감시 중 인증 확인을 계속합니다.",
            "warning",
        )
        self._install_member_session_guard(page)
        self._mark_member_session_confirmed(page)
        return True

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
            if access_token:
                self._clear_invalid_member_tokens(context)
            return self._recover_member_session(page, context)

        confirmed = self._confirm_member_session(page, context)
        if confirmed is True:
            self.log(
                "[CGV] 슬롯 1의 기존 Chrome 회원 API 인증 확인 · 현재 CGV 탭을 그대로 재사용합니다.",
                "success",
            )
            self._install_member_session_guard(page)
            self._mark_member_session_confirmed(page)
            return True

        if confirmed is False:
            self._clear_invalid_member_tokens(context)
            self.log(
                "[CGV] 저장된 회원 세션이 실제 회원 API에서 거부되어 공식 로그인 경로로 전환합니다.",
                "warning",
            )
        else:
            self.log(
                "[CGV] 저장된 회원 세션을 실시간으로 확인하지 못해 공식 로그인/확인 경로로 전환합니다.",
                "warning",
            )
        return self._recover_member_session(page, context)

    def _check_member_session_guard(self, page) -> dict[str, Any] | None:
        """Shared pre-scan gate used by BOTH parent and final watchdog paths."""
        if self.stop_event.is_set():
            self._cancel_member_session_probe(page, dispose=True)
            return {"ok": False, "status": 0, "error": "stopped", "elapsedMs": 0.0}
        if not _MEMBER_SESSION_GUARD_ACTIVE.get():
            return None
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
            if page_signature != previous_signature:
                if getattr(self, "_member_guard_page", None) is not page:
                    old_page = getattr(self, "_member_guard_page", None)
                    if old_page is not None:
                        self._cancel_member_session_probe(old_page, dispose=True)
                    self._member_guard_last_proof = getattr(self, "_member_guard_last_proof", now)
                    self._member_guard_last_start = float("-inf")
                    self._member_guard_error = getattr(self, "_member_guard_error", None)
                self._member_guard_page = page
                self._member_guard_request_id = None
                self._member_guard_completed_id = None
            last_activity = (last_read if previous_signature is not None else
                             getattr(self, "_member_guard_last_start", now))
            if math.isfinite(last_activity) and now - last_activity >= self.MEMBER_SESSION_PROOF_MAX_AGE_SECONDS:
                # Discard results spanning a host pause even if a browser clock
                # was frozen during OS sleep. Only a new post-resume probe may
                # establish new proof; the old host proof is not renewed.
                self._cancel_member_session_probe(page, dispose=True)
                self._member_guard_last_start = float("-inf")
                self._member_guard_request_id = None
                self._member_guard_completed_id = None
            self._member_guard_page_signature = page_signature
            self._member_guard_last_read = now
            start = now - getattr(self, "_member_guard_last_start", float("-inf")) >= self.MEMBER_SESSION_PROBE_INTERVAL_MS / 1000
            if start:
                self._member_guard_last_start = now
            state = self._install_member_session_guard(page, start=start)
            if self.stop_event.is_set():
                self._cancel_member_session_probe(page, dispose=True)
                return {"ok": False, "status": 0, "error": "stopped", "elapsedMs": 0.0}
            if state.get("unauthorized"):
                context = getattr(page, "context", None) or getattr(self, "_context", None)
                if now >= getattr(self, "_member_guard_retry_after", 0.0):
                    self.log(
                        "[CGV] 감시 중 회원 세션 만료를 감지했습니다. 로그인/토큰 갱신 후 감시를 계속합니다.",
                        "warning",
                    )
                if (context is None or now < getattr(self, "_member_guard_retry_after", 0.0)
                        or not self._recover_member_session(page, context)):
                    if now >= getattr(self, "_member_guard_retry_after", 0.0):
                        self._member_guard_retry_after = now + 30.0
                    self._member_guard_error = {
                        "ok": False,
                        "status": 401,
                        "statuses": [401],
                        "error": "member-session-expired",
                        "elapsedMs": 0.0,
                    }
                else:
                    self._member_guard_page_signature = None
                if self.stop_event.is_set():
                    return {"ok": False, "status": 0, "error": "stopped", "elapsedMs": 0.0}
                return self._member_guard_error
            request_id = state.get("requestId")
            if state.get("inFlight"):
                if type(request_id) is not int or request_id < 1:
                    self._cancel_member_session_probe(page, dispose=True)
                else:
                    if request_id != getattr(self, "_member_guard_request_id", None):
                        self._member_guard_request_id = request_id
                        self._member_guard_request_started = now
                    started_age = state.get("startedAgeMs")
                    if (type(started_age) in (int, float) and math.isfinite(started_age)
                            and started_age >= 0):
                        self._member_guard_request_started = min(
                            getattr(self, "_member_guard_request_started", now), now - started_age / 1000)
                    if now - getattr(self, "_member_guard_request_started", now) >= self.MEMBER_SESSION_PROBE_TIMEOUT_SECONDS:
                        self._cancel_member_session_probe(page)
            completed_id = state.get("completedId")
            if (state.get("version") == 2 and type(completed_id) is int and completed_id > 0
                    and completed_id != getattr(self, "_member_guard_completed_id", None)):
                self._member_guard_completed_id = completed_id
                completed_age = state.get("completedAgeMs")
                if (state.get("valid") is True and state.get("unauthorized") is False
                        and type(completed_age) in (int, float) and math.isfinite(completed_age)
                        and 0 <= completed_age < self.MEMBER_SESSION_PROOF_MAX_AGE_SECONDS * 1000):
                    # A result first collected after a long pause is not new
                    # authentication proof. Transfer an AGE, never subtract
                    # absolute timestamps from the browser and host clocks.
                    self._member_guard_last_proof = max(
                        getattr(self, "_member_guard_last_proof", 0.0), now - completed_age / 1000)
                    self._member_guard_error = None
            if now - getattr(self, "_member_guard_last_proof", now) >= self.MEMBER_SESSION_PROOF_MAX_AGE_SECONDS:
                if not getattr(self, "_member_guard_error", None):
                    self.log("[CGV] 회원 인증 확인이 오래 갱신되지 않았습니다 · 인증 재확인까지 신규 선점을 보류합니다.", "warning")
                self._member_guard_error = {"ok": False, "status": 0,
                    "error": "member-session-probe-stale", "elapsedMs": 0.0}
        return getattr(self, "_member_guard_error", None)

    def _race_schedule(self, page, url: str, concurrency: int) -> dict[str, Any]:
        blocked = self._check_member_session_guard(page)
        if blocked is not None:
            return blocked
        return super()._race_schedule(page, url, concurrency)

    @staticmethod
    def _serialize_structured_seat_groups(value, people: int) -> str:
        groups: list[str] = []
        seen: set[tuple[str, ...]] = set()
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
            key = tuple(seat.replace(" ", "").casefold() for seat in seats)
            if key in seen:
                continue
            seen.add(key)
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
            guard_page = getattr(self, "_member_guard_page", None)
            if guard_page is not None:
                self._cancel_member_session_probe(guard_page, dispose=True)
            self._member_guard_page = None
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
