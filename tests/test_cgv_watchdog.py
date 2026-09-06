from __future__ import annotations

import time
from types import SimpleNamespace

from engines.cgv_engine_runtime import CgvEngine as RuntimeCgvEngine
from engines.cgv_engine_watchdog import CgvEngine
from engines.registry import EngineRegistry


def _engine(logs=None):
    logs = logs if logs is not None else []
    return CgvEngine(lambda message, level: logs.append((message, level)))


def _payload(*schedules):
    return {"data": {"items": list(schedules)}}


def _schedule(
    seq: str,
    time_value: str = "1000",
    *,
    remaining: int = 100,
    controlled: str = "N",
):
    return {
        "siteNo": "0013",
        "scnYmd": "20260826",
        "scnsNo": "018",
        "scnSseq": seq,
        "scnsrtTm": time_value,
        "movNo": "30001323",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "frSeatCnt": remaining,
        "cntlYn": controlled,
    }


def test_watchdog_is_final_runtime_and_registry_uses_it():
    assert issubclass(CgvEngine, RuntimeCgvEngine)
    engine = EngineRegistry.create(
        site_name="CGV",
        mode="",
        payload={},
        custom_sites={},
        log_callback=lambda _message, _level: None,
        success_callback=None,
    )
    assert isinstance(engine, CgvEngine)


def test_quiet_watch_uses_one_request_and_burst_caps_at_two():
    engine = _engine()
    engine._schedule_watch_state = "idle"
    engine._schedule_burst_until = 0.0

    assert engine._effective_schedule_concurrency(1) == 1
    assert engine._effective_schedule_concurrency(4) == 1

    engine._schedule_burst_until = time.monotonic() + 10.0
    engine._sync_schedule_poll_interval()
    assert engine._effective_schedule_concurrency(1) == 1
    assert engine._effective_schedule_concurrency(2) == 2
    assert engine._effective_schedule_concurrency(4) == 2
    assert engine.PREOPEN_IDLE_INTERVAL == 0.5


def test_burst_restores_configured_concurrency_after_local_downshift():
    engine = _engine()
    engine.scan_concurrency = 2
    engine._schedule_burst_until = time.monotonic() + 10.0

    assert engine._effective_schedule_concurrency(1) == 2

    engine._schedule_burst_until = 0.0
    assert engine._effective_schedule_concurrency(1) == 1


def test_rate_limit_cooldown_keeps_burst_on_one_connection(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(
        "engines.cgv_engine_watchdog.time.monotonic",
        lambda: now[0],
    )
    engine = _engine()
    engine.scan_concurrency = 2
    engine._schedule_burst_until = now[0] + 120.0

    assert engine._effective_schedule_concurrency(1) == 2

    engine._update_schedule_watch_health(
        {"ok": False, "status": 429, "statuses": [429], "elapsedMs": 20}
    )
    assert engine._effective_schedule_concurrency(1) == 1

    engine._update_schedule_watch_health(
        {"ok": True, "status": 200, "statuses": [200], "data": _payload()}
    )
    assert engine._effective_schedule_concurrency(1) == 1

    now[0] += engine.SCHEDULE_RATE_LIMIT_COOLDOWN_SECONDS + 0.1
    assert engine._effective_schedule_concurrency(1) == 2


def test_schedule_fingerprint_ignores_remaining_seat_changes():
    first = CgvEngine._schedule_payload_fingerprint(
        _payload(_schedule("1", remaining=200))
    )
    second = CgvEngine._schedule_payload_fingerprint(
        _payload(_schedule("1", remaining=12))
    )
    changed = CgvEngine._schedule_payload_fingerprint(
        _payload(_schedule("1"), _schedule("2", "1300"))
    )

    assert first == second
    assert changed != first


def test_final_registry_unlock_change_reactivates_schedule_burst():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode="",
        payload={},
        custom_sites={},
        log_callback=lambda _message, _level: None,
        success_callback=None,
    )
    locked = _payload(_schedule("1", controlled=" y "))
    unlocked = _payload(_schedule("1", controlled="n"))

    engine._schedule_fingerprint = engine._schedule_payload_fingerprint(locked)
    engine._schedule_burst_until = 0.0
    engine._update_schedule_watch_health({"ok": True, "data": unlocked})

    assert engine._schedule_payload_fingerprint(locked) != engine._schedule_payload_fingerprint(
        unlocked
    )
    assert engine._schedule_burst_until > time.monotonic()
    engine._sync_schedule_poll_interval()
    assert engine.PREOPEN_IDLE_INTERVAL == engine.SCHEDULE_BURST_INTERVAL
    assert engine.SCHEDULE_HINT_INTERVAL == engine.SCHEDULE_BURST_INTERVAL


def test_schedule_identity_change_activates_short_burst():
    engine = _engine()
    engine._schedule_fingerprint = CgvEngine._schedule_payload_fingerprint(
        _payload(_schedule("1"))
    )
    engine._schedule_burst_until = 0.0

    engine._update_schedule_watch_health(
        {"ok": True, "data": _payload(_schedule("1"), _schedule("2", "1300"))}
    )

    assert engine._schedule_burst_until > time.monotonic()
    engine._sync_schedule_poll_interval()
    assert engine._effective_schedule_concurrency(4) == 2
    assert engine.PREOPEN_IDLE_INTERVAL == 0.5


def test_401_soft_refresh_retries_once(monkeypatch):
    engine = _engine()
    engine.scan_concurrency = 4
    responses = iter(
        [
            {"ok": False, "status": 401, "statuses": [401], "elapsedMs": 20},
            {"ok": True, "status": 200, "statuses": [200], "data": _payload(), "elapsedMs": 25},
        ]
    )
    calls = []

    monkeypatch.setattr(
        engine,
        "_run_schedule_race_once",
        lambda _page, _url, concurrency: calls.append(concurrency) or next(responses),
    )
    monkeypatch.setattr(engine, "_refresh_schedule_session", lambda _page: True)

    result = engine._race_schedule(SimpleNamespace(), "https://cgv.co.kr/test", 4)

    assert result["ok"] is True
    assert calls == [1, 1]


def test_json_auth_expiry_soft_refreshes_then_recovers(monkeypatch):
    engine = _engine()
    responses = iter(
        [
            {
                "ok": True,
                "status": 200,
                "statuses": [200],
                "data": {"statusCode": -1001, "data": []},
            },
            {
                "ok": True,
                "status": 200,
                "statuses": [200],
                "data": _payload(),
            },
        ]
    )
    calls = []
    monkeypatch.setattr(
        engine,
        "_run_schedule_race_once",
        lambda _page, _url, concurrency: calls.append(concurrency) or next(responses),
    )
    monkeypatch.setattr(engine, "_refresh_schedule_session", lambda _page: True)

    result = engine._race_schedule(SimpleNamespace(), "https://cgv.co.kr/test", 2)

    assert result["ok"] is True
    assert calls == [1, 1]


def test_persistent_json_auth_expiry_is_not_treated_as_empty_schedule(monkeypatch):
    engine = _engine()
    monkeypatch.setattr(
        engine,
        "_run_schedule_race_once",
        lambda *_args: {
            "ok": True,
            "status": 200,
            "statuses": [200],
            "data": {"statusCode": -1002, "data": []},
        },
    )
    monkeypatch.setattr(engine, "_refresh_schedule_session", lambda _page: False)

    result = engine._race_schedule(SimpleNamespace(), "https://cgv.co.kr/test", 2)

    assert result["ok"] is False
    assert result["status"] == 401
    assert result["unauthorized"] is True
    assert result["error"] == "schedule-session-expired"


def test_timeout_result_does_not_raise_and_can_be_retried(monkeypatch):
    engine = _engine()
    monkeypatch.setattr(
        engine,
        "_run_schedule_race_once",
        lambda _page, _url, _concurrency: {
            "ok": False,
            "status": 0,
            "statuses": [],
            "timedOut": True,
            "elapsedMs": 6000,
        },
    )

    first = engine._race_schedule(SimpleNamespace(), "https://cgv.co.kr/test", 4)
    second = engine._race_schedule(SimpleNamespace(), "https://cgv.co.kr/test", 4)

    assert first["timedOut"] is True
    assert second["timedOut"] is True
    assert engine._schedule_timeout_streak == 2


def test_hint_state_keeps_half_second_burst_without_using_four_connections():
    engine = _engine()
    engine.log("[CGV] 목표 영화 선공개 감지 · 감시 간격 단축 (2초)", "warning")

    assert engine._schedule_watch_state == "hint"
    assert engine.SCHEDULE_HINT_INTERVAL == 0.5
    assert engine._effective_schedule_concurrency(4) == 2


def test_quiet_long_watch_interval_is_not_subsecond():
    engine = _engine()
    assert engine.PREOPEN_IDLE_INTERVAL == 1.0
    assert engine.SCHEDULE_REQUEST_TIMEOUT_MS == 6000
