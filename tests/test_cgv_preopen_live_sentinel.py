from __future__ import annotations

import time

from engines.cgv_engine_funnel_runtime import CgvEngine as FunnelCgvEngine
from engines.cgv_engine_movie_identity_runtime import _PREOPEN_SELECTION_ACTIVE
from engines.cgv_engine_preopen_live_runtime import CgvEngine
from engines.registry import EngineRegistry


def _engine():
    logs: list[tuple[str, str]] = []
    engine = CgvEngine(
        log_callback=lambda message, level: logs.append((str(message), str(level))),
        success_callback=None,
    )
    engine._priority_movie = "오디세이"
    engine._priority_auditorium = "IMAX관"
    engine._priority_format = "IMAX LASER 2D"
    engine._priority_preferred_times = ["14:00"]
    return engine, logs


def _schedule(
    time_text: str = "1400",
    *,
    mov_no: str = "30001323",
    seq: str = "1",
    controlled: str = "N",
):
    return {
        "siteNo": "0013",
        "scnYmd": "20260826",
        "scnsNo": f"screen-{seq}",
        "scnSseq": seq,
        "scnsrtTm": time_text,
        "movNo": mov_no,
        "movNm": "오디세이",
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "scnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "movkndDsplNm": "IMAX LASER 2D",
        "cntlYn": controlled,
    }


def _payload(*items):
    return {"data": list(items)}


def test_registry_uses_live_sentinel_as_final_cgv_runtime():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode="",
        payload={},
        custom_sites={},
        log_callback=lambda _message, _level: None,
        success_callback=None,
    )
    assert isinstance(engine, CgvEngine)


def test_hint_state_cannot_pin_half_second_mode_after_burst_expires():
    engine, _logs = _engine()
    engine._schedule_watch_state = "hint"
    engine._schedule_burst_until = time.monotonic() - 1.0

    assert engine._schedule_burst_active() is False
    engine._sync_schedule_poll_interval()
    assert engine.PREOPEN_IDLE_INTERVAL == engine.SCHEDULE_LONG_IDLE_INTERVAL
    assert engine.SCHEDULE_HINT_INTERVAL == engine.SCHEDULE_LONG_IDLE_INTERVAL

    engine._schedule_burst_until = time.monotonic() + 5.0
    assert engine._schedule_burst_active() is True
    engine._sync_schedule_poll_interval()
    assert engine.PREOPEN_IDLE_INTERVAL == engine.SCHEDULE_BURST_INTERVAL
    assert engine.SCHEDULE_HINT_INTERVAL == engine.SCHEDULE_BURST_INTERVAL


def test_movie_no_is_recovered_from_real_reference_schedule():
    engine, _logs = _engine()
    engine._preopen_sentinel_reference_date = "20260819"
    requested_urls: list[str] = []

    def fake_fetch(_page, url, *, timeout_ms):
        requested_urls.append(str(url))
        return {"ok": True, "status": 200, "data": _payload(_schedule())}

    engine._fetch_same_origin_json = fake_fetch
    engine._maybe_discover_mov_no(
        object(),
        site_no="0013",
        target_date="20260826",
        target_payload=_payload(),
    )

    assert engine._preopen_sentinel_mov_no == "30001323"
    assert requested_urls
    assert "scnYmd=20260819" in requested_urls[0]


def test_date_sentinel_moves_from_unlisted_to_listed_and_bursts_once():
    engine, logs = _engine()
    engine._preopen_sentinel_mov_no = "30001323"
    responses = iter(
        [
            {
                "ok": True,
                "status": 200,
                "data": {"statusCode": 0, "data": [{"scnYmd": "20260825"}]},
            },
            {
                "ok": True,
                "status": 200,
                "data": {"statusCode": 0, "data": [{"scnYmd": "20260826"}]},
            },
        ]
    )
    calls: list[str] = []

    def fake_fetch(_page, url, *, timeout_ms):
        calls.append(str(url))
        return next(responses)

    engine._fetch_same_origin_json = fake_fetch
    engine._schedule_burst_until = 0.0
    engine._preopen_sentinel_last_probe = 0.0
    engine._maybe_probe_date_sentinel(
        object(), site_no="0013", target_date="20260826"
    )
    assert engine._preopen_sentinel_date_listed is False

    engine._preopen_sentinel_last_probe = 0.0
    engine._maybe_probe_date_sentinel(
        object(), site_no="0013", target_date="20260826"
    )
    assert engine._preopen_sentinel_date_listed is True
    assert engine._schedule_burst_until > time.monotonic()
    assert engine.PREOPEN_IDLE_INTERVAL == engine.SCHEDULE_BURST_INTERVAL
    assert engine.SCHEDULE_HINT_INTERVAL == engine.SCHEDULE_BURST_INTERVAL
    assert any("목표 날짜 2026-08-26 게시 감지" in message for message, _ in logs)

    # Once listed, the sentinel has completed its job and must stop producing
    # extra traffic even after the burst itself expires.
    call_count = len(calls)
    engine._schedule_burst_until = 0.0
    engine._preopen_sentinel_last_probe = 0.0
    engine._maybe_probe_date_sentinel(
        object(), site_no="0013", target_date="20260826"
    )
    assert len(calls) == call_count


def test_sentinel_error_is_fail_open_and_never_stops_schedule_watch():
    engine, logs = _engine()
    engine._preopen_sentinel_mov_no = "30001323"
    engine._fetch_same_origin_json = lambda _page, _url, *, timeout_ms: {
        "ok": False,
        "status": 429,
    }

    engine._maybe_probe_date_sentinel(
        object(), site_no="0013", target_date="20260826"
    )

    assert engine.stop_event.is_set() is False
    assert any(
        "보조 날짜 조회 일시 실패(HTTP 429)" in message
        and "기존 회차 감시는 중단하지 않고 계속" in message
        for message, _level in logs
    )


def test_real_bookable_schedule_wins_without_waiting_for_secondary_sentinel(monkeypatch):
    engine, _logs = _engine()
    payload = _payload(_schedule("1350"))
    monkeypatch.setattr(
        FunnelCgvEngine,
        "_race_schedule",
        lambda _self, _page, _url, _concurrency: {
            "ok": True,
            "status": 200,
            "data": payload,
        },
    )

    def must_not_fetch(*_args, **_kwargs):
        raise AssertionError("secondary sentinel must not delay a real booking row")

    engine._fetch_same_origin_json = must_not_fetch
    token = _PREOPEN_SELECTION_ACTIVE.set(True)
    try:
        result = engine._race_schedule(
            object(),
            CgvEngine._schedule_url("0013", "20260826"),
            2,
        )
    finally:
        _PREOPEN_SELECTION_ACTIVE.reset(token)

    assert result["ok"] is True
    assert engine._preopen_sentinel_mov_no == "30001323"
