import asyncio
import time

import pytest

import engines.keyescape_engine_observed as observed
from engines.keyescape_engine_observed import KeyescapeEngine


def run(coro):
    return asyncio.run(coro)


def make_engine():
    logs = []
    engine = KeyescapeEngine(
        log_callback=lambda message, level="info": logs.append((message, level)),
        success_callback=lambda: None,
    )
    return engine, logs


class HandlerPage:
    def __init__(self, evaluate_result=None):
        self.handlers = {}
        self.evaluate_result = evaluate_result
        self.evaluate_calls = []

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    async def evaluate(self, script, *args):
        self.evaluate_calls.append((script, args))
        return self.evaluate_result


def test_observed_runtime_keeps_proven_submit_and_fire_gate_inherited():
    assert "_submit" not in KeyescapeEngine.__dict__
    assert "_wait_for_trusted_fire" not in KeyescapeEngine.__dict__
    assert KeyescapeEngine.FIRE_LEAD == observed._ReliabilityKeyescapeEngine.FIRE_LEAD
    assert (
        KeyescapeEngine.TRUSTED_FIRE_EXTRA_SECONDS
        == observed._ReliabilityKeyescapeEngine.TRUSTED_FIRE_EXTRA_SECONDS
    )


def test_clock_resync_preserves_recent_precise_mapping_when_new_result_regresses(monkeypatch):
    engine, _logs = make_engine()
    engine.clock._apply_boundary_interval(1000.0, 10.00, 10.04)
    before = engine.clock.snapshot()
    before_precision = engine.clock.last_precision

    def coarse_resync(self, announce=False):
        del announce
        now_mono = time.monotonic()
        self.clock._anchor_monotonic = now_mono
        self.clock._anchor_server = time.time() + 5.0
        self.clock.last_precision = 0.5
        self.clock.last_offset = 5.0
        return True

    monkeypatch.setattr(
        observed._ReliabilityKeyescapeEngine,
        "_sync_server_clock",
        coarse_resync,
    )

    assert engine._sync_server_clock(announce=False) is True
    after = engine.clock.snapshot()

    assert engine.clock.last_precision == pytest.approx(before_precision)
    assert after["mapping"] == pytest.approx(before["mapping"])


def test_clock_resync_accepts_a_new_equal_or_better_mapping(monkeypatch):
    engine, _logs = make_engine()
    engine.clock._apply_boundary_interval(1000.0, 10.00, 10.08)
    before_precision = engine.clock.last_precision

    def better_resync(self, announce=False):
        del announce
        self.clock._apply_boundary_interval(1001.0, 11.03, 11.07)
        return True

    monkeypatch.setattr(
        observed._ReliabilityKeyescapeEngine,
        "_sync_server_clock",
        better_resync,
    )

    assert engine._sync_server_clock(announce=False) is True
    assert engine.clock.last_precision <= before_precision


def test_browser_prewarm_touches_exact_booking_controller():
    engine, logs = make_engine()
    page = HandlerPage(evaluate_result={
        "networkReached": True,
        "reservation": {
            "reached": True,
            "status": 200,
            "duration": 8.0,
            "dnsStart": 0.0,
            "dnsEnd": 0.0,
            "connectStart": 0.0,
            "connectEnd": 0.0,
            "secureConnectionStart": 0.0,
        },
        "controller": {
            "reached": True,
            "status": 405,
            "duration": 9.5,
            "dnsStart": 0.0,
            "dnsEnd": 0.0,
            "connectStart": 0.0,
            "connectEnd": 0.0,
            "secureConnectionStart": 0.0,
        },
    })

    assert run(engine._prewarm_browser_connection(page)) is True
    assert len(page.evaluate_calls) == 1
    _script, args = page.evaluate_calls[0]
    urls = args[0]
    assert urls["reservationUrl"].endswith("/reservation.php")
    assert urls["apiUrl"].endswith("/controller/run_proc.php")
    assert any("Chrome 예약 endpoint 예열" in message for message, _level in logs)


def test_trusted_fire_compensates_for_measured_controller_one_way_latency():
    engine, _logs = make_engine()
    engine.open_at = 1000.0
    engine.clock.last_precision = 0.0246
    engine._browser_prewarm_metrics = {
        "controller": {"reached": True, "status": 200, "duration": 44.9},
    }
    engine._browser_prewarm_observed_at = time.monotonic()

    # Apply the conservative RTT/2 estimate without crossing the opening gate.
    assert engine._trusted_fire_server_epoch() == pytest.approx(1000.00715)


@pytest.mark.parametrize("precision", [0.001, 0.020, 0.500])
@pytest.mark.parametrize("duration", [80.0, 300.0])
def test_controller_compensation_is_bounded_and_never_fires_before_open(precision, duration):
    engine, _logs = make_engine()
    engine.open_at = 1000.0
    engine.clock.last_precision = precision
    engine._browser_prewarm_metrics = {
        "controller": {"reached": True, "status": 200, "duration": duration},
    }
    engine._browser_prewarm_observed_at = time.monotonic()
    base = observed._ReliabilityKeyescapeEngine._trusted_fire_server_epoch(engine)

    target = engine._trusted_fire_server_epoch()

    assert target >= engine.open_at + engine.TRUSTED_FIRE_EXTRA_SECONDS
    assert target <= base
    assert base - target <= 0.025 + 1e-9


@pytest.mark.parametrize("changes,age", [
    ({"duration": float("nan")}, 0),
    ({"duration": float("inf")}, 0),
    ({"duration": -1}, 0),
    ({"duration": 0}, 0),
    ({"duration": 301}, 0),
    ({"duration": 3000}, 0),
    ({"status": 429}, 0),
    ({"status": 503}, 0),
    ({"reached": False}, 0),
    ({}, 16),
    ({}, -1),
    ({}, None),
])
def test_invalid_or_stale_route_measurement_keeps_original_gate(changes, age):
    engine, _logs = make_engine()
    engine.open_at = 1000.0
    engine.clock.last_precision = 0.020
    controller = {"reached": True, "status": 200, "duration": 44.9}
    controller.update(changes)
    engine._browser_prewarm_metrics = {"controller": controller}
    engine._browser_prewarm_observed_at = (
        None if age is None else time.monotonic() - age
    )

    assert engine._trusted_fire_server_epoch() == pytest.approx(1000.025)


def test_trusted_fire_keeps_original_gate_without_valid_route_measurement():
    engine, _logs = make_engine()
    engine.open_at = 1000.0
    engine.clock.last_precision = 0.020
    engine._browser_prewarm_metrics = {}

    assert engine._trusted_fire_server_epoch() == pytest.approx(1000.025)


def test_final_timing_log_reports_applied_clamped_delta_and_throttles(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(observed.time, "monotonic", lambda: now[0])
    engine, logs = make_engine()
    engine.open_at = 1000.0
    engine.clock.last_precision = 0.020
    engine._browser_prewarm_metrics = {
        "controller": {"reached": True, "status": 200, "duration": 80.0},
    }
    engine._browser_prewarm_observed_at = 98.0

    assert engine._final_post_one_way_seconds() == pytest.approx(0.025)
    assert engine._trusted_fire_server_epoch() == pytest.approx(1000.005)
    engine._trusted_fire_server_epoch()
    timing_logs = [message for message, _level in logs if "최종 타이밍" in message]
    assert len(timing_logs) == 1
    assert "적용 보정 20.0ms" in timing_logs[0]
    assert "추정 오픈 기준 목표 T+5.0ms" in timing_logs[0]
    assert "측정 나이 2.0초" in timing_logs[0]

    now[0] += 61.0
    engine._browser_prewarm_observed_at = now[0] - 1.0
    engine._trusted_fire_server_epoch()
    assert sum("최종 타이밍" in message for message, _level in logs) == 2


def test_final_timing_log_refreshes_when_late_warmup_changes_applied_value():
    engine, logs = make_engine()
    engine.open_at = 1000.0
    engine.clock.last_precision = 0.020

    engine._trusted_fire_server_epoch()
    assert "적용 보정 0.0ms" in logs[-1][0]
    assert "추정 오픈 기준 목표 T+25.0ms" in logs[-1][0]
    assert "측정 나이 없음 · 유효한 최신 측정 없음" in logs[-1][0]

    engine._browser_prewarm_metrics = {
        "controller": {"reached": True, "status": 405, "duration": 20.0},
    }
    engine._browser_prewarm_observed_at = time.monotonic()
    engine._trusted_fire_server_epoch()
    assert "적용 보정 10.0ms" in logs[-1][0]
    assert "추정 오픈 기준 목표 T+15.0ms" in logs[-1][0]
    assert sum("최종 타이밍" in message for message, _level in logs) == 2


def test_requestfinished_records_booking_post_resource_timing():
    engine, logs = make_engine()
    page = HandlerPage()
    state = engine._new_submission_state()
    run(engine._prepare_page(page, state))

    class Request:
        url = "https://www.keyescape.com/controller/run_proc.php"
        method = "POST"
        post_data = "t=ins_rev&themeTimeNum=2499"
        failure = None
        timing = {
            "startTime": 1000.0,
            "domainLookupStart": 0.0,
            "domainLookupEnd": 0.0,
            "connectStart": 1.0,
            "secureConnectionStart": 5.0,
            "connectEnd": 21.0,
            "requestStart": 23.0,
            "responseStart": 823.0,
            "responseEnd": 831.0,
        }

    assert "requestfinished" in page.handlers
    assert "request" in page.handlers
    page.handlers["request"][-1](Request())
    run(page.handlers["requestfinished"][-1](Request()))

    assert state["request_started"] is True
    assert state["request_finished"] is True
    metrics = state["network_timing"]
    assert metrics["connect_ms"] == pytest.approx(20.0)
    assert metrics["tls_ms"] == pytest.approx(16.0)
    assert metrics["request_to_first_byte_ms"] == pytest.approx(800.0)
    assert metrics["response_receive_ms"] == pytest.approx(8.0)
    assert metrics["total_ms"] == pytest.approx(831.0)
    assert any("예약 POST 네트워크 상세" in message for message, _level in logs)


def test_requestfinished_ignores_non_booking_controller_calls():
    engine, _logs = make_engine()
    page = HandlerPage()
    state = engine._new_submission_state()
    run(engine._prepare_page(page, state))

    class Request:
        url = "https://www.keyescape.com/controller/run_proc.php"
        method = "POST"
        post_data = "t=get_theme_time"
        failure = None
        timing = {"responseEnd": 10.0}

    run(page.handlers["requestfinished"][-1](Request()))
    assert "network_timing" not in state


def test_requestfailed_marks_sent_attempt_for_uncertain_reconciliation():
    engine, _logs = make_engine()
    page = HandlerPage()
    state = engine._new_submission_state()
    run(engine._prepare_page(page, state))

    class Request:
        url = "https://www.keyescape.com/controller/run_proc.php"
        method = "POST"
        post_data = "t=ins_rev&themeTimeNum=2499"
        failure = "net::ERR_TIMED_OUT"

    page.handlers["request"][-1](Request())
    run(page.handlers["requestfailed"][-1](Request()))

    assert state["request_started"] is True
    assert state["request_failed"] is True
    assert state["request_failure"] == "net::ERR_TIMED_OUT"
