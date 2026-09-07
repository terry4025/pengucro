import asyncio
import time
from unittest.mock import AsyncMock

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


def test_existing_worker_reads_later_coordinator_prewarm_and_replacements(monkeypatch):
    engine = make_engine()
    engine.open_at = 1000.0
    engine.clock.last_precision = 0.0246
    engine._live_slot_state = {"status": "pending"}
    worker = engine._make_page_worker(1)
    engine._page_workers = [worker]
    monkeypatch.setattr(engine.clock, "seconds_until", lambda _target: 1.0)
    monkeypatch.setattr(engine, "_prewarm_slot_connections", AsyncMock(return_value=2))

    class Page:
        duration = 44.9

        async def evaluate(self, _script, _urls):
            return {
                "networkReached": True,
                "controller": {"reached": True, "status": 200, "duration": self.duration},
            }

    page = Page()
    assert worker._trusted_fire_server_epoch() == pytest.approx(1000.0296)
    # Production order: construct workers first, then prewarm the coordinator.
    asyncio.run(engine._prewarm_near_open(page))
    assert worker._final_post_one_way_seconds() == pytest.approx(0.02245)
    assert worker._trusted_fire_server_epoch() == pytest.approx(1000.00715)

    first_snapshot = engine._browser_prewarm_metrics
    page.duration = 10.0
    asyncio.run(engine._prewarm_browser_connection(page))
    assert engine._browser_prewarm_metrics is not first_snapshot
    assert worker._final_post_one_way_seconds() == pytest.approx(0.005)
    assert worker._trusted_fire_server_epoch() == pytest.approx(1000.0246)

    engine._browser_prewarm_observed_at = time.monotonic() - 16
    assert worker._trusted_fire_server_epoch() == pytest.approx(1000.0296)


def test_failed_or_missing_prewarm_invalidates_worker_compensation():
    engine = make_engine()
    worker = engine._make_page_worker(1)
    engine._browser_prewarm_metrics = {
        "controller": {"reached": True, "status": 200, "duration": 44.9},
    }
    engine._browser_prewarm_observed_at = time.monotonic()

    class FailedPage:
        async def evaluate(self, *_args):
            raise RuntimeError("mock browser disconnected")

    assert worker._final_post_one_way_seconds() > 0
    assert not asyncio.run(engine._prewarm_browser_connection(FailedPage()))
    assert worker._final_post_one_way_seconds() == 0

    engine._browser_prewarm_metrics = {
        "controller": {"reached": True, "status": 200, "duration": 44.9},
    }
    engine._browser_prewarm_observed_at = time.monotonic()
    assert not asyncio.run(engine._prewarm_browser_connection(None))
    assert worker._final_post_one_way_seconds() == 0
