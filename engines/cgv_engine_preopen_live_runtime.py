from __future__ import annotations

import time
from typing import Any, Mapping

from engines.cgv_engine_preopen_sentinel_runtime import CgvEngine as SentinelCgvEngine


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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._preopen_live_reference_dates: tuple[str, ...] = ()
        self._preopen_live_reference_index = 0
        self._preopen_live_reference_pending = ""
        self._preopen_live_reference_retry_after = 0.0
        self._preopen_live_date_pending = False

    def make_reservation_thread(self, reservation_data: dict[str, Any]) -> None:
        self._preopen_live_reference_dates = ()
        self._preopen_live_reference_index = 0
        self._preopen_live_reference_pending = ""
        self._preopen_live_reference_retry_after = 0.0
        self._preopen_live_date_pending = False
        return super().make_reservation_thread(reservation_data)

    def _sync_schedule_poll_interval(self) -> None:
        super()._sync_schedule_poll_interval()
        self.SCHEDULE_HINT_INTERVAL = (
            self.SCHEDULE_BURST_INTERVAL
            if self._schedule_burst_active()
            else self.SCHEDULE_LONG_IDLE_INTERVAL
        )

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
