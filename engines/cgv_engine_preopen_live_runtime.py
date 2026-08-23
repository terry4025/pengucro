from __future__ import annotations

import ctypes
import os
import time
from typing import Any, Mapping

from engines.cgv_client import CGV_BFF_BOOKING_URL
from engines.cgv_engine_preopen_sentinel_runtime import CgvEngine as SentinelCgvEngine


_ES_SYSTEM_REQUIRED = 0x00000001
_ES_CONTINUOUS = 0x80000000
_SCHEDULE_RECOVERY_BACKOFF_KEY = "_pengucroResetScheduleBackoff"


def _set_windows_system_sleep_required(
    required: bool,
    *,
    platform_name: str | None = None,
    kernel32: Any | None = None,
) -> bool:
    """Hold or release the calling thread's Windows system-sleep request.

    The booking worker owns this request, so acquire/release happen on the same
    thread. Other platforms deliberately fail open without importing a
    platform-specific dependency.
    """

    platform = os.name if platform_name is None else str(platform_name)
    if platform != "nt":
        return False
    try:
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if required else 0)
        if kernel32 is not None:
            return bool(kernel32.SetThreadExecutionState(flags))
        function = ctypes.windll.kernel32.SetThreadExecutionState
        function.argtypes = (ctypes.c_uint,)
        function.restype = ctypes.c_uint
        return bool(function(ctypes.c_uint(flags)))
    except Exception:
        return False


class CgvEngine(SentinelCgvEngine):
    """Final CGV runtime for week-long pre-open monitoring.

    Two rules are enforced here because the watcher can stay alive for days:

    * a movie-only hint gets a bounded 0.5 s burst and then returns to the 2.5 s
      long-watch cadence even if the same regular-2D hint remains visible;
    * the auxiliary movie-id/date sentinel requests run in the page background,
      so they can never delay the next authoritative ``searchMovScnInfo`` poll.

    The real schedule feed always wins immediately. Auxiliary probes are only
    early signals and are deliberately fail-open.
    """

    PREOPEN_RESUME_GAP_SECONDS = 30.0
    PREOPEN_HEALTH_STALE_SECONDS = 90.0
    PREOPEN_AUTH_ALERT_SECONDS = 60.0
    PREOPEN_ALERT_COOLDOWN_SECONDS = 300.0
    PREOPEN_RECONNECT_ALERT_AFTER = 3

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._preopen_live_reference_dates: tuple[str, ...] = ()
        self._preopen_live_reference_index = 0
        self._preopen_live_reference_pending = ""
        self._preopen_live_reference_retry_after = 0.0
        self._preopen_live_catalog_pending = False
        self._preopen_live_date_pending = False
        self._preopen_power_request_active = False
        self._reset_unattended_health_state()

    def _reset_unattended_health_state(self) -> None:
        now = time.monotonic()
        self._preopen_health_started_at = now
        self._preopen_health_last_tick = 0.0
        self._preopen_health_last_success = 0.0
        self._preopen_health_auth_since = 0.0
        self._preopen_health_consecutive_failures = 0
        self._preopen_health_reconnect_failures = 0
        self._preopen_health_last_alert: dict[str, float] = {}
        self._preopen_health_degraded: set[str] = set()

    def make_reservation_thread(self, reservation_data: dict[str, Any]) -> None:
        self._preopen_live_reference_dates = ()
        self._preopen_live_reference_index = 0
        self._preopen_live_reference_pending = ""
        self._preopen_live_reference_retry_after = 0.0
        self._preopen_live_catalog_pending = False
        self._preopen_live_date_pending = False
        self._reset_unattended_health_state()

        self._preopen_power_request_active = _set_windows_system_sleep_required(True)
        try:
            if os.name == "nt":
                if self._preopen_power_request_active:
                    self.log(
                        "[CGV] 장시간 감시 중 Windows 시스템 절전 방지를 활성화했습니다.",
                        "info",
                    )
                else:
                    self.log(
                        "[CGV][무인 감시 경보] Windows 절전 방지를 활성화하지 못했습니다. "
                        "전원 설정에서 절전·최대절전을 직접 꺼주세요.",
                        "error",
                    )
            return super().make_reservation_thread(reservation_data)
        finally:
            if self._preopen_power_request_active:
                released = _set_windows_system_sleep_required(False)
                self._preopen_power_request_active = False
                if not released:
                    self.log(
                        "[CGV] Windows 절전 방지 요청 해제 확인에 실패했습니다.",
                        "warning",
                    )

    @staticmethod
    def _schedule_result_is_healthy(result: Mapping[str, Any]) -> bool:
        if not result.get("ok") or int(result.get("status", 0) or 0) != 200:
            return False
        payload = result.get("data")
        if isinstance(payload, Mapping):
            try:
                return int(payload.get("statusCode", 0) or 0) == 0
            except (TypeError, ValueError):
                return False
        return False

    @staticmethod
    def _schedule_result_is_unauthorized(result: Mapping[str, Any]) -> bool:
        statuses = {
            int(value or 0)
            for value in result.get("statuses", ())
            if str(value or "").strip()
        }
        status = int(result.get("status", 0) or 0)
        payload = result.get("data")
        api_status = 0
        if isinstance(payload, Mapping):
            try:
                api_status = int(payload.get("statusCode", 0) or 0)
            except (TypeError, ValueError):
                api_status = 0
        return status == 401 or 401 in statuses or api_status in {-1001, -1002}

    def _audible_operational_alert(self) -> bool:
        if os.name != "nt":
            return False
        try:
            import winsound

            winsound.PlaySound(
                "SystemExclamation",
                winsound.SND_ALIAS | winsound.SND_ASYNC,
            )
            return True
        except Exception:
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                return True
            except Exception:
                return False

    def _emit_operational_alert(self, key: str, message: str) -> bool:
        now = time.monotonic()
        last = self._preopen_health_last_alert.get(str(key))
        if last is not None and now - last < self.PREOPEN_ALERT_COOLDOWN_SECONDS:
            return False
        self._preopen_health_last_alert[str(key)] = now
        self._preopen_health_degraded.add(str(key))
        # Bypass this class's log observer to avoid treating the generated alert
        # itself as another reconnect/health signal.
        super().log(f"[CGV][무인 감시 경보] {message}", "error")
        self._audible_operational_alert()
        return True

    def _clear_operational_alert(self, key: str, message: str) -> None:
        if str(key) not in self._preopen_health_degraded:
            return
        self._preopen_health_degraded.discard(str(key))
        self._preopen_health_last_alert.pop(str(key), None)
        super().log(f"[CGV][무인 감시 복구] {message}", "success")

    def _health_age(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else float(now)
        anchor = self._preopen_health_last_success or self._preopen_health_started_at
        return max(0.0, current - anchor)

    def _update_schedule_watch_health(self, result: dict[str, Any]) -> None:
        now = time.monotonic()
        if self._schedule_result_is_healthy(result):
            recovered = bool(
                self._preopen_health_degraded.intersection({"auth", "stale", "network"})
            )
            self._preopen_health_last_success = now
            self._preopen_health_auth_since = 0.0
            self._preopen_health_consecutive_failures = 0
            for key in ("auth", "stale", "network"):
                self._preopen_health_degraded.discard(key)
                self._preopen_health_last_alert.pop(key, None)
            if recovered:
                super().log(
                    "[CGV][무인 감시 복구] 정상 200 회차 응답을 다시 확인했습니다.",
                    "success",
                )
        else:
            self._preopen_health_consecutive_failures += 1
            unauthorized = self._schedule_result_is_unauthorized(result)
            if unauthorized:
                if self._preopen_health_auth_since <= 0:
                    self._preopen_health_auth_since = now
                if now - self._preopen_health_auth_since >= self.PREOPEN_AUTH_ALERT_SECONDS:
                    self._emit_operational_alert(
                        "auth",
                        "CGV 로그인 인증 만료가 지속되고 있습니다. "
                        "열린 Chrome 슬롯 1에서 즉시 로그인해주세요.",
                    )

            age = self._health_age(now)
            if age >= self.PREOPEN_HEALTH_STALE_SECONDS:
                reason = (
                    "로그인 인증 실패"
                    if unauthorized
                    else "네트워크 또는 CGV 회차 조회 실패"
                )
                self._emit_operational_alert(
                    "auth" if unauthorized else "stale",
                    f"정상 200 회차 응답이 {age:.0f}초 동안 없습니다 · {reason}.",
                )

        # The watchdog owns fingerprint/burst/rate-limit accounting. Our state
        # is updated first so its periodic health log can never report a stale
        # watcher as healthy.
        return super()._update_schedule_watch_health(result)

    def _prepare_resume_recovery(self, page, gap_seconds: float) -> None:
        self._schedule_last_auth_refresh = 0.0
        self._preopen_live_reference_pending = ""
        self._preopen_live_date_pending = False
        try:
            page.evaluate(
                "() => { delete window.__pengucroPreopenAux; return true; }"
            )
        except Exception:
            # The authoritative request immediately below is the real CDP/page
            # liveness check and will enter the existing reconnect path if dead.
            pass
        self._activate_schedule_burst(
            "절전·네트워크 중단 후 복귀 감지",
            seconds=max(90.0, self.DATE_SENTINEL_BURST_SECONDS),
            log_transition=False,
        )
        super().log(
            f"[CGV] 장시간 실행 공백 {gap_seconds:.0f}초 감지 · "
            "backoff 초기화 신호, 세션 재검증 및 즉시 집중 감시를 시작합니다.",
            "warning",
        )

    def _race_schedule(self, page, url: str, concurrency: int) -> dict[str, Any]:
        now = time.monotonic()
        previous_tick = self._preopen_health_last_tick
        gap_seconds = max(0.0, now - previous_tick) if previous_tick > 0 else 0.0
        resume_recovery = gap_seconds >= self.PREOPEN_RESUME_GAP_SECONDS
        self._preopen_health_last_tick = now
        if resume_recovery:
            self._prepare_resume_recovery(page, gap_seconds)

        result = super()._race_schedule(page, url, concurrency)
        result = dict(result) if isinstance(result, Mapping) else {
            "ok": False,
            "status": 0,
            "error": "invalid-schedule-result",
        }
        if resume_recovery:
            result[_SCHEDULE_RECOVERY_BACKOFF_KEY] = True
        return result

    def log(self, message: str, level: str = "info") -> None:
        text = str(message or "")
        if text.startswith("[CGV] 장기 감시 정상 동작 중"):
            age = self._health_age()
            if age >= self.PREOPEN_HEALTH_STALE_SECONDS:
                self._emit_operational_alert(
                    "auth" if "auth" in self._preopen_health_degraded else "stale",
                    f"정상 200 회차 응답이 {age:.0f}초 동안 없어 "
                    "정상 heartbeat를 표시하지 않습니다.",
                )
                return
            text = f"{text} · 최근 정상 200 응답 {age:.0f}초 전"

        reconnect_failure = (
            "브라우저 재연결 대기 중" in text
            or "좌석 단계 브라우저 재연결 대기/실패" in text
        )
        if reconnect_failure:
            self._preopen_health_reconnect_failures += 1
            if (
                self._preopen_health_reconnect_failures
                >= self.PREOPEN_RECONNECT_ALERT_AFTER
            ):
                self._emit_operational_alert(
                    "reconnect",
                    "CGV Chrome 자동 재연결이 반복 실패했습니다. "
                    "슬롯 1 Chrome과 네트워크 상태를 확인해주세요.",
                )
        elif "브라우저 재연결 성공" in text or "Chrome 프로세스 재시작 및 좌석 화면 복구 성공" in text:
            self._preopen_health_reconnect_failures = 0
            self._clear_operational_alert(
                "reconnect", "CGV Chrome 자동 재연결에 성공했습니다."
            )

        return super().log(text, level)

    def _sync_schedule_poll_interval(self) -> None:
        super()._sync_schedule_poll_interval()
        if self._schedule_burst_active():
            interval = self.SCHEDULE_BURST_INTERVAL
        elif self._preopen_sentinel_date_listed is True:
            interval = self.DATE_LISTED_INTERVAL_SECONDS
        else:
            interval = self.SCHEDULE_LONG_IDLE_INTERVAL
        self.PREOPEN_IDLE_INTERVAL = interval
        self.SCHEDULE_HINT_INTERVAL = interval

    def _activate_schedule_burst(
        self,
        reason: str,
        *,
        seconds: float | None = None,
        log_transition: bool = True,
    ) -> None:
        super()._activate_schedule_burst(
            reason,
            seconds=seconds,
            log_transition=log_transition,
        )
        # The base loop chooses its next sleep immediately after the current
        # response. Synchronize now so the first hint/sentinel signal gets the
        # 0.5 s cadence without waiting through another 2.5 s idle cycle.
        self._sync_schedule_poll_interval()

    @staticmethod
    def _background_json_step(
        page,
        *,
        key: str,
        url: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Start/poll one same-origin GET without awaiting network completion.

        ``page.evaluate`` itself returns immediately after installing the async
        request. A later authoritative schedule tick consumes the stored result.
        No helper request can therefore sit between two real schedule polls.
        """

        try:
            result = page.evaluate(
                r"""
                ({key, url, timeoutMs}) => {
                  const root = window.__pengucroPreopenAux ||
                    (window.__pengucroPreopenAux = Object.create(null));
                  const previous = root[key];
                  if (previous && previous.done) {
                    const value = previous.result || {
                      ok: false, status: 0, error: 'missing-background-result'
                    };
                    delete root[key];
                    return {state: 'done', result: value};
                  }
                  if (previous && previous.running) return {state: 'running'};

                  const state = {running: true, done: false, result: null};
                  root[key] = state;
                  void (async () => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), timeoutMs);
                    try {
                      const headers = new Headers({
                        'Accept': 'application/json, text/plain, */*',
                        'Accept-Language': 'ko-KR',
                      });
                      const item = String(document.cookie || '').split('; ')
                        .find(value => value.startsWith('accessToken='));
                      if (item) {
                        let token = item.slice('accessToken='.length);
                        try { token = decodeURIComponent(token); } catch (_) {}
                        if (token) headers.set('Authorization', `Bearer ${token}`);
                      }
                      const response = await fetch(url, {
                        method: 'GET',
                        cache: 'no-store',
                        credentials: 'include',
                        headers,
                        signal: controller.signal,
                      });
                      let data = null;
                      try { data = await response.json(); } catch (_) {}
                      state.result = {ok: response.ok, status: response.status, data};
                    } catch (error) {
                      state.result = {
                        ok: false,
                        status: 0,
                        timedOut: Boolean(error && error.name === 'AbortError'),
                        error: String(error || 'fetch failed'),
                      };
                    } finally {
                      clearTimeout(timer);
                      state.running = false;
                      state.done = true;
                    }
                  })();
                  return {state: 'started'};
                }
                """,
                {
                    "key": str(key),
                    "url": str(url),
                    "timeoutMs": max(500, int(timeout_ms)),
                },
            )
        except Exception as exc:
            return {
                "state": "done",
                "result": {"ok": False, "status": 0, "error": str(exc)},
            }
        return dict(result) if isinstance(result, dict) else {
            "state": "done",
            "result": {"ok": False, "status": 0, "error": "invalid-background-state"},
        }

    def _maybe_discover_mov_no(
        self,
        page,
        *,
        site_no: str,
        target_date: str,
        target_payload: Mapping[str, Any] | None,
    ) -> None:
        if self._preopen_sentinel_mov_no:
            self._preopen_live_reference_pending = ""
            return

        if isinstance(target_payload, Mapping):
            mov_no = self._extract_target_mov_no(target_payload)
            if self._remember_mov_no(mov_no, source="목표 날짜 회차 응답"):
                self._preopen_live_reference_pending = ""
                return

        movie = str(getattr(self, "_priority_movie", "") or "").strip()
        now = time.monotonic()
        catalog_due = (
            self._preopen_sentinel_last_catalog_discovery <= 0
            or now - self._preopen_sentinel_last_catalog_discovery
            >= self.MOVIE_NO_CATALOG_DISCOVERY_INTERVAL_SECONDS
        )
        if movie and (self._preopen_live_catalog_pending or catalog_due):
            if not self._preopen_live_catalog_pending:
                self._preopen_sentinel_last_catalog_discovery = now
            step = self._background_json_step(
                page,
                key=f"catalog:{movie}",
                url=(
                    f"{CGV_BFF_BOOKING_URL}/searchAtktTopPostrList?"
                    f"{self._movie_catalog_query(movie)}"
                ),
                timeout_ms=self.DATE_SENTINEL_TIMEOUT_MS,
            )
            state = str(step.get("state") or "")
            if state in {"started", "running"}:
                self._preopen_live_catalog_pending = True
                return
            self._preopen_live_catalog_pending = False
            result = step.get("result")
            result = dict(result) if isinstance(result, Mapping) else {}
            data = result.get("data")
            if result.get("ok") and isinstance(data, Mapping):
                mov_no = self._extract_catalog_mov_no(
                    data,
                    movie=movie,
                    format_name=str(getattr(self, "_priority_format", "") or ""),
                )
                if self._remember_mov_no(mov_no, source="예매 영화 목록 조회"):
                    self._preopen_live_reference_pending = ""
                    return

        now = time.monotonic()
        if (
            not self._preopen_live_reference_pending
            and now < self._preopen_live_reference_retry_after
        ):
            return

        if not self._preopen_live_reference_dates:
            self._preopen_live_reference_dates = self._reference_dates(target_date)
            self._preopen_live_reference_index = 0

        if self._preopen_live_reference_pending:
            reference_date = self._preopen_live_reference_pending
        else:
            if self._preopen_live_reference_index >= len(self._preopen_live_reference_dates):
                self._preopen_live_reference_dates = ()
                self._preopen_live_reference_index = 0
                self._preopen_live_reference_retry_after = (
                    now + self.MOVIE_NO_DISCOVERY_INTERVAL_SECONDS
                )
                return
            reference_date = self._preopen_live_reference_dates[
                self._preopen_live_reference_index
            ]

        step = self._background_json_step(
            page,
            key=f"movno:{site_no}:{reference_date}",
            url=self._schedule_url(site_no, reference_date),
            timeout_ms=self.DATE_SENTINEL_TIMEOUT_MS,
        )
        state = str(step.get("state") or "")
        if state in {"started", "running"}:
            self._preopen_live_reference_pending = reference_date
            return

        self._preopen_live_reference_pending = ""
        result = step.get("result")
        result = dict(result) if isinstance(result, Mapping) else {}
        data = result.get("data")
        if result.get("ok") and isinstance(data, Mapping):
            mov_no = self._extract_target_mov_no(data)
            if self._remember_mov_no(
                mov_no,
                source=(
                    f"참고 날짜 {reference_date[:4]}-{reference_date[4:6]}-"
                    f"{reference_date[6:]}"
                ),
            ):
                return

        self._preopen_live_reference_index += 1
        if self._preopen_live_reference_index >= len(self._preopen_live_reference_dates):
            self._preopen_live_reference_dates = ()
            self._preopen_live_reference_index = 0
            self._preopen_live_reference_retry_after = (
                time.monotonic() + self.MOVIE_NO_DISCOVERY_INTERVAL_SECONDS
            )

    def _consume_date_sentinel_result(
        self,
        result: Mapping[str, Any],
        *,
        target_date: str,
        mov_no: str,
    ) -> None:
        data = result.get("data")
        if not result.get("ok") or not isinstance(data, Mapping):
            self._log_sentinel_error(result)
            return

        rows = data.get("data")
        if not isinstance(rows, list):
            rows = []
        published_dates = {
            self._date_digits(item.get("scnYmd"))
            for item in rows
            if isinstance(item, Mapping)
        }
        published_dates.discard("")
        listed = target_date in published_dates
        previous = self._preopen_sentinel_date_listed
        self._preopen_sentinel_date_listed = listed

        target_label = (
            f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"
            if len(target_date) == 8
            else target_date
        )
        if listed and previous is not True:
            self._activate_schedule_burst(
                "영화별 상영일 목록에서 목표 날짜 게시 감지",
                seconds=self.DATE_SENTINEL_BURST_SECONDS,
                log_transition=False,
            )
            self.log(
                f"[CGV][미오픈 sentinel] 목표 날짜 {target_label} 게시 감지 · "
                f"movNo={mov_no} · {self.DATE_SENTINEL_BURST_SECONDS:.0f}초 동안 "
                "0.5초 실제 회차 집중 감시로 전환합니다.",
                "success",
            )
        elif previous is None:
            self.log(
                f"[CGV][미오픈 sentinel] 준비 완료 · movNo={mov_no} · "
                f"목표 날짜 {target_label}는 아직 영화별 상영일 목록에 없습니다. "
                "장기 감시를 계속합니다.",
                "info",
            )

    def _maybe_probe_date_sentinel(
        self,
        page,
        *,
        site_no: str,
        target_date: str,
    ) -> None:
        mov_no = str(self._preopen_sentinel_mov_no or "").strip()
        if not mov_no or not site_no or not target_date:
            self._preopen_live_date_pending = False
            return

        # Once observed, the date endpoint has completed its only job. Never
        # keep this helper running for the remaining hours/days.
        if self._preopen_sentinel_date_listed is True:
            self._preopen_live_date_pending = False
            return

        # If a helper request is already in flight, poll its tiny JS state on
        # every real schedule tick. This does not wait for the network request.
        if self._preopen_live_date_pending:
            step = self._background_json_step(
                page,
                key=f"date:{site_no}:{mov_no}",
                url=self._date_sentinel_url(site_no, mov_no),
                timeout_ms=self.DATE_SENTINEL_TIMEOUT_MS,
            )
            state = str(step.get("state") or "")
            if state in {"started", "running"}:
                return
            self._preopen_live_date_pending = False
            result = step.get("result")
            self._consume_date_sentinel_result(
                dict(result) if isinstance(result, Mapping) else {},
                target_date=target_date,
                mov_no=mov_no,
            )
            return

        # While the authoritative watcher is already in its short 0.5 s burst,
        # another early-warning request adds no value.
        if self._schedule_burst_active():
            return

        now = time.monotonic()
        if (
            self._preopen_sentinel_last_probe > 0
            and now - self._preopen_sentinel_last_probe
            < self.DATE_SENTINEL_INTERVAL_SECONDS
        ):
            return
        self._preopen_sentinel_last_probe = now

        step = self._background_json_step(
            page,
            key=f"date:{site_no}:{mov_no}",
            url=self._date_sentinel_url(site_no, mov_no),
            timeout_ms=self.DATE_SENTINEL_TIMEOUT_MS,
        )
        state = str(step.get("state") or "")
        if state in {"started", "running"}:
            self._preopen_live_date_pending = True
            return

        result = step.get("result")
        self._consume_date_sentinel_result(
            dict(result) if isinstance(result, Mapping) else {},
            target_date=target_date,
            mov_no=mov_no,
        )
