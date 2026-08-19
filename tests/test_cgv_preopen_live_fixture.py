from __future__ import annotations

import json
from pathlib import Path

from engines.cgv_engine_preopen_live_runtime import CgvEngine


def test_captured_search_site_date_list_response_is_understood():
    fixture = Path(__file__).parent / "fixtures" / "cgv_search_site_dates_odyssey.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    engine = CgvEngine(log_callback=lambda *_args: None, success_callback=None)
    engine._preopen_sentinel_mov_no = "30001323"
    engine._preopen_sentinel_date_listed = None
    logs: list[tuple[str, str]] = []
    engine.log = lambda message, level="info": logs.append((str(message), str(level)))

    engine._consume_date_sentinel_result(
        {"ok": True, "status": 200, "data": payload},
        target_date="20260825",
        mov_no="30001323",
    )

    assert engine._preopen_sentinel_date_listed is True
    assert any("목표 날짜 2026-08-25 게시 감지" in message for message, _ in logs)


def test_captured_search_site_date_list_does_not_claim_unpublished_next_day():
    fixture = Path(__file__).parent / "fixtures" / "cgv_search_site_dates_odyssey.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    engine = CgvEngine(log_callback=lambda *_args: None, success_callback=None)
    engine._preopen_sentinel_date_listed = None
    engine._consume_date_sentinel_result(
        {"ok": True, "status": 200, "data": payload},
        target_date="20260826",
        mov_no="30001323",
    )

    assert engine._preopen_sentinel_date_listed is False
