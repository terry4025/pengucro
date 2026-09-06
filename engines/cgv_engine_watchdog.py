from __future__ import annotations

import time
from typing import Any, Mapping

from engines.cgv_client import schedule_items
from engines.cgv_engine_runtime import CgvEngine as RuntimeCgvEngine
from engines.cgv_schedule_observer import run_schedule_wave


class CgvEngine(RuntimeCgvEngine):
    """Long-running CGV schedule watcher layered over the fast runtime.

    The booking/seat/hold path remains owned by ``cgv_engine_runtime``. This
    layer only changes the pre-open schedule watcher so it can stay alive for
    hours or days without holding the user's maximum hedge count open
    continuously.

    Policy:
    * quiet unpublished dates use one request wave every second;
    * target-movie hints or a real schedule-list change enable a short 0.5-second
      burst and at most two hedged requests;
    * one schedule race has a hard six-second deadline so a broken fetch cannot
      pin the watcher forever;
    * a 401 receives one soft same-tab refresh/retry. If CGV really requires a
      fresh login, the normal loop remains alive and keeps backing off rather
      than terminating the reservation task.
    """

    SCHEDULE_LONG_IDLE_INTERVAL = 1.0
    SCHEDULE_BURST_INTERVAL = 0.5
    PREOPEN_IDLE_INTERVAL = SCHEDULE_LONG_IDLE_INTERVAL
    SCHEDULE_HINT_INTERVAL = SCHEDULE_BURST_INTERVAL

    SCHEDULE_REQUEST_TIMEOUT_MS = 6000
    SCHEDULE_BURST_SECONDS = 45.0
    SCHEDULE_BURST_MAX_CONCURRENCY = 2
    SCHEDULE_AUTH_REFRESH_COOLDOWN_SECONDS = 30.0
    SCHEDULE_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
    SCHEDULE_HEALTH_LOG_INTERVAL_SECONDS = 1800.0

    def __init__(self, log_callback, success_callback=None, **kwargs) -> None:
        super().__init__(log_callback, success_callback, **kwargs)
        self._schedule_watch_state = "idle"
        self._schedule_burst_until = 0.0
        self._schedule_fingerprint: tuple[tuple[str, ...], ...] | None = None
        self._schedule_last_auth_refresh = 0.0
        self._schedule_rate_limit_until = 0.0
        self._schedule_last_health_log = time.monotonic()
        self._schedule_timeout_streak = 0

    @staticmethod
    def _schedule_payload_fingerprint(payload: Any) -> tuple[tuple[str, ...], ...]:
        """Track schedule identities, not changing remaining-seat counters."""

        if not isinstance(payload, dict):
            return ()
        identities: list[tuple[str, ...]] = []
        for item in schedule_items(payload):
            identities.append(
                (
                    str(item.get("siteNo", "") or ""),
                    str(item.get("scnYmd", "") or ""),
                    str(item.get("scnsNo", "") or ""),
                    str(item.get("scnSseq", "") or ""),
                    str(item.get("scnsrtTm", "") or ""),
                    str(item.get("movNo") or item.get("prodNo") or ""),
                    str(item.get("expoScnsNm") or item.get("scnsNm") or ""),
                    str(item.get("movkndDsplEnm") or item.get("movkndDsplNm") or ""),
                    str(item.get("cntlYn", "N") or "N").strip().upper(),
                )
            )
        return tuple(sorted(set(identities)))

    def _activate_schedule_burst(
        self,
        reason: str,
        *,
        seconds: float | None = None,
        log_transition: bool = True,
    ) -> None:
        now = time.monotonic()
        duration = max(1.0, float(seconds or self.SCHEDULE_BURST_SECONDS))
        was_active = now < self._schedule_burst_until
        self._schedule_burst_until = max(self._schedule_burst_until, now + duration)
        if log_transition and not was_active:
            self.log(
                f"[CGV] {reason} · {duration:.0f}초 동안 0.5초 고속 회차 감시로 전환합니다.",
                "info",
            )

    def _schedule_burst_active(self) -> bool:
        return (
            self._schedule_watch_state == "hint"
            or time.monotonic() < self._schedule_burst_until
        )

    def _sync_schedule_poll_interval(self) -> None:
        self.PREOPEN_IDLE_INTERVAL = (
            self.SCHEDULE_BURST_INTERVAL
            if self._schedule_burst_active()
            else self.SCHEDULE_LONG_IDLE_INTERVAL
        )

    def _effective_schedule_concurrency(self, requested: int) -> int:
        requested = max(1, int(requested or 1))
        if time.monotonic() < self._schedule_rate_limit_until:
            return 1
        if not self._schedule_burst_active():
            return 1
        # A slow response can temporarily reduce the local loop variable to one.
        # Once an actual opening hint starts a bounded burst, restore the user's
        # configured hedge count so that the recovery is real (and matches the
        # health log) instead of remaining permanently downshifted.
        configured = max(1, int(getattr(self, "scan_concurrency", requested) or 1))
        return min(
            max(requested, configured),
            int(self.SCHEDULE_BURST_MAX_CONCURRENCY),
        )

    def _run_schedule_race_once(
        self,
        page,
        url: str,
        concurrency: int,
    ) -> dict[str, Any]:
        """Run one bounded same-origin hedge wave."""

        script = r"""
        async ({url, concurrency, hedgeDelayMs, hardTimeoutMs, observerKey}) => {
          const started = performance.now();
          const controllers = Array.from(
            {length: concurrency}, () => new AbortController()
          );
          const statuses = [];
          const timers = [];
          const dispatches = [];
          let failed = 0;
          let settled = false;
          let hardTimer = null;

          const requestHeaders = () => {
            const headers = new Headers({
              'Accept': 'application/json, text/plain, */*',
              'Accept-Language': 'ko-KR'
            });
            const item = String(document.cookie || '').split('; ')
              .find(value => value.startsWith('accessToken='));
            if (item) {
              const raw = item.slice('accessToken='.length);
              let token = raw;
              try { token = decodeURIComponent(raw); } catch (_) {}
              if (token) headers.set('Authorization', `Bearer ${token}`);
            }
            return headers;
          };

          return await new Promise((resolve) => {
            const finish = (value, winner = -1) => {
              if (settled) return;
              settled = true;
              for (const timer of timers) clearTimeout(timer);
              if (hardTimer) clearTimeout(hardTimer);
              controllers.forEach((controller, index) => {
                if (index !== winner) controller.abort();
              });
              resolve(value);
            };

            const launch = (controller, index) => {
              if (settled) return;
              dispatches.push({index, delayMs: performance.now() - started,
                               visibility: document.visibilityState || 'unknown'});
              fetch(url, {
                method: 'GET',
                cache: 'no-store',
                credentials: 'include',
                headers: requestHeaders(),
                signal: controller.signal
              }).then(async (response) => {
                statuses.push(response.status);
                if ([401, 403, 429].includes(response.status)) {
                  finish({ok: false, status: response.status, statuses,
                          elapsedMs: performance.now() - started}, index);
                  return;
                }
                if (!response.ok) throw new Error(String(response.status));
                const data = await response.json();
                finish({
                  ok: true,
                  status: response.status,
                  data,
                  statuses,
                  elapsedMs: performance.now() - started,
                  effectiveConcurrency: concurrency,
                  dispatches,
                }, index);
              }).catch((error) => {
                if (settled || (error && error.name === 'AbortError')) return;
                failed += 1;
                if (failed >= concurrency) {
                  finish({
                    ok: false,
                    status: statuses[0] || 0,
                    statuses,
                    error: String(error || 'schedule fetch failed'),
                    elapsedMs: performance.now() - started,
                    effectiveConcurrency: concurrency,
                  });
                }
              });
            };

            controllers.forEach((controller, index) => {
              if (index === 0) launch(controller, index);
              else timers.push(setTimeout(() => launch(controller, index), index * hedgeDelayMs));
            });

            const entry = (window.__pengucroScheduleWaves || {})[observerKey];
            if (entry) entry.cancel = () => finish({ok: false, status: 0, error: 'schedule-host-cancel'});

            hardTimer = setTimeout(() => {
              finish({
                ok: false,
                status: 0,
                statuses,
                timedOut: true,
                error: 'schedule-timeout',
                elapsedMs: performance.now() - started,
                effectiveConcurrency: concurrency,
              });
            }, hardTimeoutMs);
          });
        }
        """

        result = run_schedule_wave(
            page, script,
            {
                "url": url,
                "concurrency": max(1, int(concurrency)),
                "hedgeDelayMs": self.HEDGE_DELAY_MS,
                "hardTimeoutMs": self.SCHEDULE_REQUEST_TIMEOUT_MS,
            },
            self.stop_event, self.SCHEDULE_REQUEST_TIMEOUT_MS / 1000,
        )
        return (
            dict(result)
            if isinstance(result, dict)
            else {"ok": False, "status": 0, "error": "invalid-schedule-result"}
        )

    def _refresh_schedule_session(self, page) -> bool:
        """Give CGV's own app one chance to renew cookies after a 401."""

        now = time.monotonic()
        if (
            now - self._schedule_last_auth_refresh
            < self.SCHEDULE_AUTH_REFRESH_COOLDOWN_SECONDS
        ):
            return False
        self._schedule_last_auth_refresh = now

        self.log(
            "[CGV] 회차 조회 인증 만료 신호(401) · 슬롯 1의 현재 CGV 탭을 새로고침해 세션 갱신을 시도합니다.",
            "warning",
        )
        try:
            page.reload(wait_until="domcontentloaded", timeout=15000)
            try:
                page.wait_for_timeout(300)
            except Exception:
                pass
            if "/mem/login" in str(getattr(page, "url", "") or ""):
                self.log(
                    "[CGV] 로그인 세션이 완전히 만료되었습니다. 감시는 종료하지 않고 유지합니다. "
                    "열린 슬롯 1 Chrome에서 로그인하면 다음 조회부터 자동으로 재개됩니다.",
                    "warning",
                )
                return False
            return True
        except Exception as exc:
            if self._is_recoverable_browser_error(exc):
                raise
            self.silent_tick("CGV 세션 갱신 대기 · 장기 감시는 계속 유지합니다")
            return False

    @staticmethod
    def _schedule_api_status(result: Mapping[str, Any]) -> int:
        payload = result.get("data")
        if not isinstance(payload, Mapping):
            return 0
        try:
            return int(payload.get("statusCode", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _schedule_result_is_unauthorized(cls, result: Mapping[str, Any]) -> bool:
        status = int(result.get("status", 0) or 0)
        statuses = {
            int(value or 0)
            for value in result.get("statuses", [])
            if str(value or "").strip()
        }
        return (
            status == 401
            or 401 in statuses
            or cls._schedule_api_status(result) in {-1001, -1002}
        )

    @staticmethod
    def _normalize_schedule_auth_failure(result: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(result)
        statuses = [
            int(value or 0)
            for value in result.get("statuses", [])
            if str(value or "").strip()
        ]
        if 401 not in statuses:
            statuses.append(401)
        normalized.update(
            {
                "ok": False,
                "status": 401,
                "statuses": statuses,
                "unauthorized": True,
                "error": "schedule-session-expired",
            }
        )
        return normalized

    def _update_schedule_watch_health(self, result: dict[str, Any]) -> None:
        now = time.monotonic()
        status = int(result.get("status", 0) or 0)
        statuses = {
            int(value or 0)
            for value in result.get("statuses", [])
            if str(value or "").strip()
        }
        if status in {403, 429} or statuses.intersection({403, 429}):
            # A server protection response is different from a merely slow
            # request. Keep the watcher on one connection for a bounded period
            # even when an opening burst is active; another protection signal
            # extends the cooldown.
            self._schedule_rate_limit_until = max(
                self._schedule_rate_limit_until,
                now + self.SCHEDULE_RATE_LIMIT_COOLDOWN_SECONDS,
            )
        if result.get("ok"):
            self._schedule_timeout_streak = 0
            payload = result.get("data")
            fingerprint = self._schedule_payload_fingerprint(payload)
            previous = self._schedule_fingerprint
            self._schedule_fingerprint = fingerprint
            if previous is not None and fingerprint != previous:
                self._activate_schedule_burst("시간표 구성 변화 감지")
        elif result.get("timedOut"):
            self._schedule_timeout_streak += 1
            if self._schedule_timeout_streak in {1, 3}:
                self.silent_tick(
                    f"CGV 회차 조회 응답 지연 · {self.SCHEDULE_REQUEST_TIMEOUT_MS // 1000}초 제한으로 "
                    "멈춤을 방지하고 자동 재시도합니다"
                )

        if (
            now - self._schedule_last_health_log
            >= self.SCHEDULE_HEALTH_LOG_INTERVAL_SECONDS
        ):
            self._schedule_last_health_log = now
            mode = (
                "고속"
                if self._effective_schedule_concurrency(max(1, self.scan_concurrency)) > 1
                else "저부하"
            )
            self.log(
                f"[CGV] 장기 감시 정상 동작 중 · {mode} 모드 · "
                f"Chrome 슬롯 1 · 요청 timeout {self.SCHEDULE_REQUEST_TIMEOUT_MS // 1000}초",
                "info",
            )

    def _race_schedule(self, page, url: str, concurrency: int) -> dict[str, Any]:
        """Adaptive long-running schedule race.

        The user's 1-4 slider remains an upper bound. Quiet monitoring uses one
        request. During a useful burst, at most two hedged requests are used
        because third/fourth hedges add traffic only after 220/330 ms without
        improving the common fast-response case.
        """

        self._sync_schedule_poll_interval()
        effective = self._effective_schedule_concurrency(concurrency)
        result = self._run_schedule_race_once(page, url, effective)

        unauthorized = self._schedule_result_is_unauthorized(result)
        if unauthorized and not self.stop_event.is_set():
            if self._refresh_schedule_session(page):
                result = self._run_schedule_race_once(page, url, effective)

        if self._schedule_result_is_unauthorized(result):
            result = self._normalize_schedule_auth_failure(result)

        self._update_schedule_watch_health(result)
        self._sync_schedule_poll_interval()
        return result

    def log(self, message: str, level: str = "info") -> None:
        if message == "[CGV] 목표 영화 선공개 감지 · 감시 간격 단축 (2초)":
            self._schedule_watch_state = "hint"
            self._activate_schedule_burst(
                "목표 영화 선공개 감지",
                seconds=max(self.SCHEDULE_BURST_SECONDS, 90.0),
                log_transition=False,
            )
        elif message == "[CGV] 미오픈 대기 · 20초 간격으로 시간표 확인":
            self._schedule_watch_state = "idle"
        elif "실제 IMAX 회차 감지" in message:
            self._schedule_watch_state = "open"

        super().log(message, level)
