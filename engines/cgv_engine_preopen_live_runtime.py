from __future__ import annotations

from engines.cgv_engine_preopen_sentinel_runtime import CgvEngine as SentinelCgvEngine


class CgvEngine(SentinelCgvEngine):
    """Final CGV runtime for week-long pre-open monitoring.

    The base reservation loop reads ``SCHEDULE_HINT_INTERVAL`` directly while a
    target-movie hint remains visible.  Therefore the watchdog timer must govern
    both the ordinary pre-open interval and the hint interval; otherwise a 2D
    hint can pin the watcher at 0.5 s indefinitely.
    """

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
        # The base loop may choose its next sleep immediately after the current
        # response. Synchronize now so the very first hint/sentinel signal gets
        # 0.5 s polling without waiting through one more 2.5 s idle cycle.
        self._sync_schedule_poll_interval()
