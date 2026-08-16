from __future__ import annotations

from typing import Any

from engines.cgv_engine import CgvEngine as BaseCgvEngine
from engines.cgv_engine_hardened import CgvEngine as HardenedCgvEngine


class CgvEngine(HardenedCgvEngine):
    """Final CGV runtime policy for fast-monitor failure handling.

    The fast browser-side seat monitor is useful while CGV accepts the polling
    rate, but retrying the same endpoint with multi-second exponential backoff
    after an explicit 403/429 leaves an already-open seat UI idle.  Convert
    those terminal monitor states into the base engine's existing safe-fallback
    path instead.  This preserves the direct-hold success path unchanged.
    """

    # 120 ms sustained polling proved aggressive enough to enter CGV's
    # connection-limit path during real runs.  180 ms keeps sub-second detection
    # while materially reducing sustained request pressure.
    FAST_SEAT_LAUNCH_INTERVAL_MS = 180

    def __init__(self, log_callback, success_callback=None, **kwargs) -> None:
        super().__init__(log_callback, success_callback, **kwargs)
        self._fast_monitor_fallback_reason = ""

    def _read_fast_seat_monitor(self, page) -> dict[str, Any]:
        snapshot = BaseCgvEngine._read_fast_seat_monitor(page)

        # Losing the JS monitor state means the fast path can no longer make a
        # trustworthy decision.  Do not sleep/restart the same path; use the
        # browser seat map that is already open.
        if not snapshot:
            self._fast_monitor_fallback_reason = "state-lost"
            return {
                "running": False,
                "terminalError": "monitor-state-lost",
                "lastStatus": 0,
            }

        status = int(snapshot.get("lastStatus", 0) or 0)
        blocked = bool(snapshot.get("blocked")) or status in {403, 429}
        stopped_by_fetch_errors = (
            not snapshot.get("running", False)
            and not snapshot.get("hit")
            and int(snapshot.get("consecutiveErrors", 0) or 0)
            >= self.FAST_MONITOR_MAX_CONSECUTIVE_ERRORS
        )

        if blocked:
            self._fast_monitor_fallback_reason = "rate-limited"
            snapshot["terminalError"] = "rate-limited"
        elif stopped_by_fetch_errors:
            self._fast_monitor_fallback_reason = "fetch-errors"
            snapshot["terminalError"] = "consecutive-fetch-errors"

        return snapshot

    def log(self, message: str, level: str = "info") -> None:
        # Base _watch_and_hold_api already treats terminalError as an immediate
        # safe fallback.  Translate that one generic message so production logs
        # state the real reason instead of claiming the response schema changed.
        generic_terminal = (
            "CGV 선점 API 응답 구조가 변경되어 브라우저 안전 경로로 전환합니다."
        )
        if message == generic_terminal and self._fast_monitor_fallback_reason:
            reason = self._fast_monitor_fallback_reason
            self._fast_monitor_fallback_reason = ""
            if reason == "rate-limited":
                message = (
                    "CGV 고속 좌석 API 연결 제한 감지 · 백오프 재시도 없이 "
                    "브라우저 좌석 감시로 즉시 전환합니다."
                )
            elif reason == "fetch-errors":
                message = (
                    "CGV 고속 좌석 API 연속 조회 실패 · 같은 API 재시도 대신 "
                    "브라우저 좌석 감시로 즉시 전환합니다."
                )
            else:
                message = (
                    "CGV 고속 좌석 감시 상태를 읽지 못해 "
                    "브라우저 좌석 감시로 즉시 전환합니다."
                )

        super().log(message, level)
