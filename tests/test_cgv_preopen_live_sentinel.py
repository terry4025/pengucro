from __future__ import annotations

import time

import pytest

from engines.cgv_engine_funnel_runtime import CgvEngine as FunnelCgvEngine
from engines.cgv_engine_movie_identity_runtime import _PREOPEN_SELECTION_ACTIVE
from engines.cgv_engine_preopen_live_runtime import (
    CgvEngine,
    _ES_CONTINUOUS,
    _ES_SYSTEM_REQUIRED,
    _set_windows_system_sleep_required,
)
from engines.cgv_engine_preopen_sentinel_runtime import CgvEngine as SentinelCgvEngine
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


def test_windows_sleep_request_is_thread_scoped_and_non_windows_fails_open():
    class Kernel32:
        def __init__(self):
            self.calls = []

        def SetThreadExecutionState(self, flags):
            self.calls.append(int(flags))
            return 1

    kernel32 = Kernel32()

    assert (
        _set_windows_system_sleep_required(
            True, platform_name="posix", kernel32=kernel32
        )
        is False
    )
    assert kernel32.calls == []

    assert _set_windows_system_sleep_required(
        True, platform_name="nt", kernel32=kernel32
    )
    assert _set_windows_system_sleep_required(
        False, platform_name="nt", kernel32=kernel32
    )
    assert kernel32.calls == [
        _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED,
        _ES_CONTINUOUS,
    ]


def test_booking_task_releases_sleep_request_even_when_parent_flow_raises(monkeypatch):
    engine, _logs = _engine()
    power_calls = []

    monkeypatch.setattr(
        "engines.cgv_engine_preopen_live_runtime._set_windows_system_sleep_required",
        lambda required: power_calls.append(bool(required)) or True,
    )
    monkeypatch.setattr(
        SentinelCgvEngine,
        "make_reservation_thread",
        lambda _self, _data: (_ for _ in ()).throw(RuntimeError("flow failed")),
    )

    with pytest.raises(RuntimeError, match="flow failed"):
        engine.make_reservation_thread({})

    assert power_calls == [True, False]
    assert engine._preopen_power_request_active is False


def test_long_tick_gap_arms_resume_burst_and_backoff_reset_signal(monkeypatch):
    engine, logs = _engine()
    engine._preopen_health_last_tick = time.monotonic() - 45.0
    engine._schedule_last_auth_refresh = time.monotonic()

    monkeypatch.setattr(
        FunnelCgvEngine,
        "_race_schedule",
        lambda _self, _page, _url, _concurrency: {
            "ok": True,
            "status": 200,
            "data": {"statusCode": 0, "data": []},
        },
    )

    class Page:
        def __init__(self):
            self.scripts = []

        def evaluate(self, script):
            self.scripts.append(str(script))
            return True

    page = Page()
    result = engine._race_schedule(page, "https://cgv.co.kr/test", 1)

    assert result["_pengucroResetScheduleBackoff"] is True
    assert engine._schedule_last_auth_refresh == 0.0
    assert engine._schedule_burst_until > time.monotonic()
    assert any("__pengucroPreopenAux" in script for script in page.scripts)
    assert any("backoff 초기화 신호" in message for message, _level in logs)


def test_stale_health_never_emits_false_green_and_alert_is_rate_limited(monkeypatch):
    engine, logs = _engine()
    now = [1000.0]
    beeps = []
    monkeypatch.setattr(
        "engines.cgv_engine_preopen_live_runtime.time.monotonic",
        lambda: now[0],
    )
    monkeypatch.setattr(
        engine,
        "_audible_operational_alert",
        lambda: beeps.append(now[0]) or True,
    )
    engine._preopen_health_started_at = 900.0
    engine._preopen_health_last_success = 0.0

    failed = {
        "ok": False,
        "status": 0,
        "statuses": [],
        "timedOut": True,
    }
    engine._update_schedule_watch_health(failed)
    engine.log(
        "[CGV] 장기 감시 정상 동작 중 · 저부하 모드 · Chrome 슬롯 1",
        "info",
    )
    now[0] += 1.0
    engine._update_schedule_watch_health(failed)

    assert not any("장기 감시 정상 동작 중" in message for message, _ in logs)
    alerts = [
        message for message, level in logs
        if "무인 감시 경보" in message and level == "error"
    ]
    assert len(alerts) == 1
    assert len(beeps) == 1

    now[0] += 1.0
    engine._update_schedule_watch_health(
        {
            "ok": True,
            "status": 200,
            "statuses": [200],
            "data": {"statusCode": 0, "data": []},
        }
    )
    engine.log(
        "[CGV] 장기 감시 정상 동작 중 · 저부하 모드 · Chrome 슬롯 1",
        "info",
    )

    assert any("정상 200 회차 응답을 다시 확인" in message for message, _ in logs)
    assert any("최근 정상 200 응답" in message for message, _ in logs)


def test_persistent_auth_and_reconnect_failures_raise_audible_alerts(monkeypatch):
    engine, logs = _engine()
    now = [2000.0]
    beeps = []
    monkeypatch.setattr(
        "engines.cgv_engine_preopen_live_runtime.time.monotonic",
        lambda: now[0],
    )
    monkeypatch.setattr(
        engine,
        "_audible_operational_alert",
        lambda: beeps.append(now[0]) or True,
    )
    engine._preopen_health_started_at = now[0]

    unauthorized = {
        "ok": False,
        "status": 401,
        "statuses": [401],
        "data": None,
    }
    engine._update_schedule_watch_health(unauthorized)
    now[0] += engine.PREOPEN_AUTH_ALERT_SECONDS + 0.1
    engine._update_schedule_watch_health(unauthorized)

    for _ in range(engine.PREOPEN_RECONNECT_ALERT_AFTER):
        engine.log("[CGV] 브라우저 재연결 대기 중... (CDP disconnected)", "warning")

    assert any("로그인 인증 만료가 지속" in message for message, _ in logs)
    assert any("Chrome 자동 재연결이 반복 실패" in message for message, _ in logs)
    assert len(beeps) == 2

    engine.log("[CGV] 브라우저 재연결 성공 · 미오픈 감시를 계속합니다.", "success")
    assert engine._preopen_health_reconnect_failures == 0
    assert any("Chrome 자동 재연결에 성공" in message for message, _ in logs)


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


def test_listed_date_keeps_middle_cadence_after_short_burst_expires():
    engine, _logs = _engine()
    engine._preopen_sentinel_date_listed = True
    engine._schedule_watch_state = "idle"
    engine._schedule_burst_until = time.monotonic() - 1.0

    engine._sync_schedule_poll_interval()

    assert engine.PREOPEN_IDLE_INTERVAL == engine.DATE_LISTED_INTERVAL_SECONDS
    assert engine.SCHEDULE_HINT_INTERVAL == engine.DATE_LISTED_INTERVAL_SECONDS
    assert (
        engine.SCHEDULE_BURST_INTERVAL
        < engine.DATE_LISTED_INTERVAL_SECONDS
        < engine.SCHEDULE_LONG_IDLE_INTERVAL
    )


def test_catalog_movie_id_requires_one_exact_title_match():
    payload = {
        "data": [
            {"movNo": "wrong", "movNm": "오디세이: 감독판"},
            {"movNo": "target", "movNm": "오디세이"},
        ]
    }

    assert CgvEngine._extract_catalog_mov_no(
        payload,
        movie="오디세이",
        format_name="IMAX LASER 2D",
    ) == "target"

    ambiguous = {
        "data": [
            {"movNo": "target-a", "movNm": "오디세이"},
            {"movNo": "target-b", "movNm": "오디세이"},
        ]
    }
    assert CgvEngine._extract_catalog_mov_no(
        ambiguous,
        movie="오디세이",
        format_name="IMAX LASER 2D",
    ) == ""


def test_final_runtime_discovers_catalog_movie_id_without_blocking_schedule():
    engine, _logs = _engine()
    states = iter(
        [
            {"state": "started"},
            {
                "state": "done",
                "result": {
                    "ok": True,
                    "status": 200,
                    "data": {
                        "statusCode": 0,
                        "data": [
                            {"movNo": "wrong", "movNm": "오디세이: 감독판"},
                            {"movNo": "30001323", "movNm": "오디세이"},
                        ],
                    },
                },
            },
        ]
    )
    requested_urls = []

    def fake_step(_page, *, key, url, timeout_ms):
        requested_urls.append(str(url))
        return next(states)

    engine._background_json_step = fake_step
    for _ in range(2):
        engine._maybe_discover_mov_no(
            object(),
            site_no="0013",
            target_date="20260826",
            target_payload=_payload(),
        )

    assert engine._preopen_sentinel_mov_no == "30001323"
    assert any("searchAtktTopPostrList" in url for url in requested_urls)


def test_movie_no_is_recovered_from_reference_schedule_without_waiting_on_fetch():
    engine, _logs = _engine()
    engine._preopen_sentinel_reference_date = "20260819"
    requested_urls: list[str] = []

    def fake_step(_page, *, key, url, timeout_ms):
        requested_urls.append(str(url))
        if "searchAtktTopPostrList" in str(url):
            return {
                "state": "done",
                "result": {
                    "ok": True,
                    "status": 200,
                    "data": {"statusCode": 0, "data": []},
                },
            }
        return {
            "state": "done",
            "result": {"ok": True, "status": 200, "data": _payload(_schedule())},
        }

    engine._background_json_step = fake_step
    engine._maybe_discover_mov_no(
        object(),
        site_no="0013",
        target_date="20260826",
        target_payload=_payload(),
    )

    assert engine._preopen_sentinel_mov_no == "30001323"
    assert requested_urls
    assert any("scnYmd=20260819" in url for url in requested_urls)


def test_reference_movie_id_discovery_can_span_multiple_async_ticks():
    engine, _logs = _engine()
    engine._preopen_sentinel_reference_date = "20260819"
    states = iter(
        [
            {"state": "started"},
            {"state": "running"},
            {
                "state": "done",
                "result": {
                    "ok": True,
                    "status": 200,
                    "data": _payload(_schedule()),
                },
            },
        ]
    )
    engine._background_json_step = lambda *_args, **_kwargs: next(states)

    for _ in range(3):
        engine._maybe_discover_mov_no(
            object(),
            site_no="0013",
            target_date="20260826",
            target_payload=_payload(),
        )

    assert engine._preopen_sentinel_mov_no == "30001323"
    assert engine._preopen_live_reference_pending == ""


def test_date_sentinel_runs_in_background_then_bursts_when_date_appears():
    engine, logs = _engine()
    engine._preopen_sentinel_mov_no = "30001323"
    states = iter(
        [
            {"state": "started"},
            {"state": "running"},
            {
                "state": "done",
                "result": {
                    "ok": True,
                    "status": 200,
                    "data": {
                        "statusCode": 0,
                        "data": [{"scnYmd": "20260826"}],
                    },
                },
            },
        ]
    )
    calls: list[str] = []

    def fake_step(_page, *, key, url, timeout_ms):
        calls.append(str(url))
        return next(states)

    engine._background_json_step = fake_step
    engine._schedule_burst_until = 0.0
    engine._preopen_sentinel_last_probe = 0.0

    engine._maybe_probe_date_sentinel(
        object(), site_no="0013", target_date="20260826"
    )
    assert engine._preopen_live_date_pending is True
    assert engine._preopen_sentinel_date_listed is None

    engine._maybe_probe_date_sentinel(
        object(), site_no="0013", target_date="20260826"
    )
    assert engine._preopen_live_date_pending is True

    engine._maybe_probe_date_sentinel(
        object(), site_no="0013", target_date="20260826"
    )
    assert engine._preopen_live_date_pending is False
    assert engine._preopen_sentinel_date_listed is True
    assert engine._schedule_burst_until > time.monotonic()
    assert engine.PREOPEN_IDLE_INTERVAL == engine.SCHEDULE_BURST_INTERVAL
    assert engine.SCHEDULE_HINT_INTERVAL == engine.SCHEDULE_BURST_INTERVAL
    assert any("목표 날짜 2026-08-26 게시 감지" in message for message, _ in logs)

    # Once listed, the helper has completed its job and never creates more date
    # traffic, even after the short high-speed burst expires.
    call_count = len(calls)
    engine._schedule_burst_until = 0.0
    engine._preopen_sentinel_last_probe = 0.0
    engine._maybe_probe_date_sentinel(
        object(), site_no="0013", target_date="20260826"
    )
    assert len(calls) == call_count


def test_unlisted_date_is_rechecked_after_interval_without_stopping_main_watch():
    engine, logs = _engine()
    engine._preopen_sentinel_mov_no = "30001323"
    engine._background_json_step = lambda *_args, **_kwargs: {
        "state": "done",
        "result": {
            "ok": True,
            "status": 200,
            "data": {"statusCode": 0, "data": [{"scnYmd": "20260825"}]},
        },
    }

    engine._preopen_sentinel_last_probe = 0.0
    engine._maybe_probe_date_sentinel(
        object(), site_no="0013", target_date="20260826"
    )

    assert engine._preopen_sentinel_date_listed is False
    assert engine.stop_event.is_set() is False
    assert any("아직 영화별 상영일 목록에 없습니다" in message for message, _ in logs)


def test_sentinel_error_is_fail_open_and_never_stops_schedule_watch():
    engine, logs = _engine()
    engine._preopen_sentinel_mov_no = "30001323"
    engine._background_json_step = lambda *_args, **_kwargs: {
        "state": "done",
        "result": {"ok": False, "status": 429},
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


def test_real_bookable_schedule_wins_without_running_secondary_probe(monkeypatch):
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

    def must_not_probe(*_args, **_kwargs):
        raise AssertionError("secondary probe must not delay a real booking row")

    engine._background_json_step = must_not_probe
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
