import asyncio
from types import SimpleNamespace

from engines.keyescape_engine_single_page import KeyescapeEngine


def noop(*args, **kwargs):
    return None


def make_engine():
    return KeyescapeEngine(log_callback=noop, success_callback=noop)


def test_trusted_single_page_does_not_spawn_a_second_browser_page():
    engine = make_engine()
    engine._page_count = 1
    engine._trusted_slot_id = "1410"

    armed = engine._ensure_parallel_live_fallback(already_open=False)

    assert armed is True
    assert engine._page_count == 1
    assert engine._single_page_http_fallback is True


def test_explicit_multi_page_mode_is_left_unchanged():
    engine = make_engine()
    engine._page_count = 2
    engine._trusted_slot_id = "1410"

    armed = engine._ensure_parallel_live_fallback(already_open=False)

    assert armed is False
    assert engine._page_count == 2
    assert engine._single_page_http_fallback is False


def test_http_validation_replaces_stale_trusted_slot_without_new_page():
    engine = make_engine()
    engine._page_count = 1
    engine._trusted_slot_id = "1410"
    engine._trusted_slot_sources = ("2026-08-23",)
    engine._single_page_http_fallback = True
    engine._live_slot_state = {
        "target_date": "2026-08-30",
        "zizum_num": "10",
        "theme_num": "77",
        "timing_key": "10:77:13:50",
    }
    worker = SimpleNamespace(
        _trusted_slot_id="1410",
        _trusted_slot_sources=("2026-08-23",),
    )
    engine._page_workers = [worker]

    async def no_wait():
        return None

    async def resolve(*args, **kwargs):
        return "1411", "ready"

    engine._wait_for_http_validation_window = no_wait
    engine._resolve_live_slot = resolve

    asyncio.run(engine._validate_trusted_slot_http())

    assert engine._page_count == 1
    assert engine._trusted_slot_id == "1411"
    assert worker._trusted_slot_id == "1411"
    assert engine._trusted_slot_sources == ("실시간 HTTP 검증",)


def test_http_capacity_clears_trusted_fast_fire_candidate():
    engine = make_engine()
    engine._page_count = 1
    engine._trusted_slot_id = "1410"
    engine._trusted_slot_sources = ("2026-08-23",)
    engine._single_page_http_fallback = True
    engine._live_slot_state = {
        "target_date": "2026-08-30",
        "zizum_num": "10",
        "theme_num": "77",
        "timing_key": "10:77:13:50",
    }
    worker = SimpleNamespace(
        _trusted_slot_id="1410",
        _trusted_slot_sources=("2026-08-23",),
    )
    engine._page_workers = [worker]

    async def no_wait():
        return None

    async def resolve(*args, **kwargs):
        return "1410", "capacity"

    engine._wait_for_http_validation_window = no_wait
    engine._resolve_live_slot = resolve

    asyncio.run(engine._validate_trusted_slot_http())

    assert engine._page_count == 1
    assert engine._trusted_slot_id == ""
    assert worker._trusted_slot_id == ""
