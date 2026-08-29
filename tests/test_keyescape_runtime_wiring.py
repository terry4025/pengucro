import time

import pytest

import engines.keyescape_engine_runtime as runtime
from engines.keyescape_engine_runtime import KeyescapeEngine


def make_engine():
    return KeyescapeEngine(
        log_callback=lambda *_args, **_kwargs: None,
        success_callback=lambda: None,
    )


def test_page_workers_keep_active_runtime_class_and_shared_state():
    engine = make_engine()
    engine._page_count = 3
    engine._trusted_slot_id = "2499"
    engine._trusted_slot_sources = ("2026-08-20", "2026-08-19")
    engine._live_slot_state = {"status": "pending"}

    worker1 = engine._make_page_worker(1)
    worker2 = engine._make_page_worker(2)

    assert type(worker1) is KeyescapeEngine
    assert type(worker2) is KeyescapeEngine
    assert worker1.clock is engine.clock
    assert worker2.clock is engine.clock
    assert worker1._live_slot_state is engine._live_slot_state
    assert worker2._live_slot_state is engine._live_slot_state
    assert worker1._trusted_slot_id == "2499"
    assert worker2._trusted_slot_id == ""
    assert worker1._clock_sync_enabled is True
    assert worker2._clock_sync_enabled is False


def test_runtime_clock_preserves_recent_real_precise_sample(monkeypatch):
    engine = make_engine()
    engine.clock._apply_boundary_interval(1000.0, 10.00, 10.04)
    before_anchor_monotonic = engine.clock._anchor_monotonic
    before_anchor_server = engine.clock._anchor_server
    before_intervals = list(engine.clock._mapping_intervals)
    before_precision = engine.clock.last_precision
    before_offset = engine.clock.last_offset

    def coarse_sync(self, announce=False):
        del announce
        now_mono = time.monotonic()
        self.clock._anchor_monotonic = now_mono
        self.clock._anchor_server = time.time() + 5.0
        self.clock.last_precision = 0.5
        self.clock.last_offset = 5.0
        self.clock._mapping_intervals.append((1.0, 2.0, now_mono))
        return True

    monkeypatch.setattr(
        runtime._ReliabilityKeyescapeEngine,
        "_sync_server_clock",
        coarse_sync,
    )

    assert engine._sync_server_clock(announce=False) is True
    assert engine.clock._anchor_monotonic == before_anchor_monotonic
    assert engine.clock._anchor_server == before_anchor_server
    assert engine.clock._mapping_intervals == before_intervals
    assert engine.clock.last_precision == pytest.approx(before_precision)
    assert engine.clock.last_offset == pytest.approx(before_offset)


def test_runtime_does_not_override_final_submit_or_fire_wait_loop():
    assert "_submit" not in KeyescapeEngine.__dict__
    assert "_wait_for_trusted_fire" not in KeyescapeEngine.__dict__
