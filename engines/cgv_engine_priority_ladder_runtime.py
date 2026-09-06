from __future__ import annotations

from typing import Any
import time

from engines.cgv_engine_priority_ladder import CgvEngine as PriorityLadderCgvEngine
from engines.cgv_schedule_observer import ScheduleObserver


class CgvEngine(PriorityLadderCgvEngine):
    """Final priority-ladder runtime preserving member seat-query context."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._priority_seed_page = None
        self._priority_observer = None
        self._priority_schedule_blocked = False

    def _refresh_priority_schedule_payload(self, page) -> None:
        if not self._priority_schedule_url or self._priority_schedule_blocked:
            return
        if self._priority_observer is None:
            self._priority_observer = ScheduleObserver(
                self._priority_schedule_url,
                next_due=self._priority_last_schedule_refresh + ScheduleObserver.INTERVAL_SECONDS)
        result = self._priority_observer.step(page)
        if not result:
            return
        data = result.get("data")
        status = int(result.get("status", 0) or 0)
        if isinstance(data, dict) and str(data.get("statusCode", 0)) in {"-1001", "-1002"}:
            status = 401
        if status in {401, 403, 429} or result.get("observerLost"):
            self._priority_schedule_blocked = True
            self._last_fast_monitor_exit_reason = "schedule-observer-blocked"
            self.log(f"[CGV] 회차 갱신 제한/연결 이상 (HTTP {status}) · 추가 자동 선점 중단", "warning")
            return
        if result.get("ok") and isinstance(data, dict) and str(data.get("statusCode", 0)) == "0":
            self._priority_schedule_payload = dict(data)
            self._priority_last_schedule_refresh = time.monotonic()

    def _seed_initial_payload(
        self, schedule: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        page = self._priority_seed_page
        auth = self._browser_auth_data(page) if page is not None else {}
        self._initial_seat_response = {
            "url": self._seat_url(schedule, auth.get("custNo", "")),
            "status": 200,
            "data": payload,
            "requestHeaders": {},
        }

    def _watch_and_hold_api(self, page, schedule, groups, people, developer_mode, cgv):
        self._priority_seed_page = page
        self._priority_schedule_blocked = False
        try:
            return super()._watch_and_hold_api(
                page, schedule, groups, people, developer_mode, cgv
            )
        finally:
            if self._priority_observer is not None:
                self._priority_observer.close(page)
            self._priority_observer = None
            self._priority_seed_page = None
