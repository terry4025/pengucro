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


def test_runtime_does_not_override_final_submit_or_fire_gate():
    assert "_submit" not in KeyescapeEngine.__dict__
    assert "_wait_for_trusted_fire" not in KeyescapeEngine.__dict__
