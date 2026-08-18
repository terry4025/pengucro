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
    run(page.handlers["requestfinished"][-1](Request()))

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
