from __future__ import annotations

from typing import Any

from engines.cgv_engine_priority_ladder import CgvEngine as PriorityLadderCgvEngine


class CgvEngine(PriorityLadderCgvEngine):
    """Final priority-ladder runtime preserving member seat-query context."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._priority_seed_page = None

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
        try:
            return super()._watch_and_hold_api(
                page, schedule, groups, people, developer_mode, cgv
            )
        finally:
            self._priority_seed_page = None
