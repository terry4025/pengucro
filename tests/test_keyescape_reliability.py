import asyncio
from types import SimpleNamespace

import pytest

import engines.keyescape_engine_single_page as runtime
from engines.keyescape_engine_single_page import KeyescapeEngine


def make_engine():
    return KeyescapeEngine(log_callback=lambda *_args: None, success_callback=lambda: None)


def run(coro):
    return asyncio.run(coro)


def test_reliability_wrapper_does_not_override_submit_or_trusted_fire_gate():
    # This PR may improve observation/recovery, but it must not move the proven
    # Fast Path fire point or replace the final browser submission routine.
    assert "_submit" not in KeyescapeEngine.__dict__
    assert "_wait_for_trusted_fire" not in KeyescapeEngine.__dict__
    assert KeyescapeEngine.FIRE_LEAD == runtime._BaseKeyescapeEngine.FIRE_LEAD
    assert (
        KeyescapeEngine.TRUSTED_FIRE_EXTRA_SECONDS
        == runtime._BaseKeyescapeEngine.TRUSTED_FIRE_EXTRA_SECONDS
    )


def test_timing_parameters_only_make_first_read_more_conservative():
    samples = [
        {"read_rtt_ms": 100.0},
        {"read_rtt_ms": 200.0},
        {"read_rtt_ms": 700.0},
        {"read_rtt_ms": 900.0},
    ]
    base = runtime._BaseKeyescapeEngine._timing_parameters(samples)
    hardened = KeyescapeEngine._timing_parameters(samples)

    assert hardened[0] == base[0]
    assert hardened[1] == base[1]
    assert hardened[2] >= base[2]
    assert hardened[2] <= KeyescapeEngine.SLOT_READ_LEAD_MAX_SECONDS


def test_sparse_exact_timing_uses_theme_and_branch_fallback(monkeypatch):
    engine = make_engine()
    history = {
        "18:58:13:40": {"samples": [{"read_rtt_ms": 100.0}]},
        "18:58:14:40": {"samples": [{"read_rtt_ms": 900.0}]},
        "18:58:15:40": {"samples": [{"read_rtt_ms": 1000.0}]},
        "22:65:15:50": {"samples": [{"read_rtt_ms": 50.0}]},
    }
    monkeypatch.setattr(runtime, "load_json", lambda *_args, **_kwargs: history)

    key, _hedge, _retry, read_lead, has_timing = engine._load_timing_profile(
        "18", "58", "13:40"
    )

    assert key == "18:58:13:40"
    assert has_timing is True
    assert read_lead >= 0.45


def test_adaptive_shared_wait_keeps_existing_absolute_cap():
    engine = make_engine()
    engine._live_slot_state = {"read_lead": 0.25}

    wait_seconds = engine._adaptive_shared_wait_seconds()

    assert engine.SHARED_WAIT_MIN_SECONDS <= wait_seconds
    assert wait_seconds < engine.SHARED_SLOT_WAIT_SECONDS


def test_follower_uses_adaptive_wait_then_falls_back_to_own_read():
    engine = make_engine()
    engine._live_slot_state = {"read_lead": 0.25}
    observed = {}

    class Share:
        owner = False

        def wait_for_result(self, timeout):
            observed["timeout"] = timeout
            return []

    async def own_read(*_args):
        observed["own_read"] = True
        return [{"num": "2301", "hh": "9", "mm": "50", "enable": "Y"}]

    engine._slot_share = Share()
    engine._fetch_live_slots = own_read

    rows = run(engine._fetch_coordinated_live_slots(
        "2026-08-24", "22", "65", "09:50"
    ))

    assert observed["timeout"] == pytest.approx(
        engine._adaptive_shared_wait_seconds(), rel=0.01
    )
    assert observed["own_read"] is True
    assert engine._slot_share is None
    assert rows[0]["num"] == "2301"


def test_trusted_mismatch_quarantines_only_single_template_relaxation(monkeypatch):
    engine = make_engine()
    store = {"version": 1, "entries": {}}

    monkeypatch.setattr(runtime, "load_json", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(runtime, "save_json", lambda _name, value: store.update(value))

    engine._record_trusted_mismatch(
        "2026-08-30", "10", "77", "1410", "1411"
    )
    assert engine._single_template_quarantined("2026-08-30", "10", "77") is True

    monkeypatch.setattr(
        runtime._BaseKeyescapeEngine,
        "_trusted_slot_from_cache",
        lambda *_args, **_kwargs: ("1410", ("2026-08-23",)),
    )
    assert engine._trusted_slot_from_cache(
        "2026-08-30", "13:50", "10", "77"
    ) == ("", ())


def test_two_date_template_rebuilds_trust_after_mismatch(monkeypatch):
    engine = make_engine()
    key = engine._trusted_health_key("2026-08-24", "18", "55")
    store = {
        "version": 1,
        "entries": {
            key: {
                "mismatch_count": 1,
                "observed_at": "2026-08-18T11:00:00+09:00",
                "trusted_id": "1797",
                "live_id": "1798",
            }
        },
    }

    monkeypatch.setattr(runtime, "load_json", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(runtime, "save_json", lambda _name, value: store.update(value))
    monkeypatch.setattr(
        runtime._BaseKeyescapeEngine,
        "_trusted_slot_from_cache",
        lambda *_args, **_kwargs: (
            "1798", ("2026-08-20", "2026-08-19")
        ),
    )

    slot_id, sources = engine._trusted_slot_from_cache(
        "2026-08-24", "19:50", "18", "55"
    )

    assert slot_id == "1798"
    assert len(sources) == 2
    assert key not in store["entries"]


def test_live_timing_records_trusted_mismatch_for_multi_page_runs(monkeypatch):
    engine = make_engine()
    engine._trusted_slot_id = "2498"
    engine.open_at = 100.0
    engine.clock = SimpleNamespace(now=lambda: 100.70)
    store = {"version": 1, "entries": {}}

    monkeypatch.setattr(runtime, "load_json", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(runtime, "save_json", lambda _name, value: store.update(value))

    def fake_base_remember(self, state):
        state["timing_sample"] = {
            "read_rtt_ms": 700.0,
            "publish_delay_ms": 700.0,
        }

    monkeypatch.setattr(
        runtime._BaseKeyescapeEngine,
        "_remember_slot_timing",
        fake_base_remember,
    )
    state = {
        "slot_id": "2499",
        "target_date": "2026-08-24",
        "zizum_num": "23",
        "theme_num": "88",
    }

    engine._remember_slot_timing(state)

    key = engine._trusted_health_key("2026-08-24", "23", "88")
    assert state["trusted_mismatch_recorded"] is True
    assert store["entries"][key]["trusted_id"] == "2498"
    assert store["entries"][key]["live_id"] == "2499"


def test_http_validator_starts_independently_of_browser_prewarm(monkeypatch):
    engine = make_engine()
    engine._single_page_http_fallback = True
    engine._page_count = 1
    engine._trusted_slot_id = "1410"
    validator_started = asyncio.Event()

    async def fake_validator():
        validator_started.set()

    async def fake_base_prewarm(self, page=None):
        await asyncio.wait_for(validator_started.wait(), timeout=0.1)

    monkeypatch.setattr(engine, "_validate_trusted_slot_http", fake_validator)
    monkeypatch.setattr(
        runtime._BaseKeyescapeEngine,
        "_prewarm_near_open",
        fake_base_prewarm,
    )

    run(engine._prewarm_near_open(None))
    assert validator_started.is_set()


def test_timing_sample_records_boundary_observation_without_changing_submit(monkeypatch):
    engine = make_engine()
    engine._live_slot_state = {}
    engine.open_at = 100.0
    engine.clock = SimpleNamespace(now=lambda: 100.42)

    def fake_base_remember(self, state):
        state["timing_sample"] = {
            "read_rtt_ms": 510.0,
            "publish_delay_ms": 420.0,
        }

    monkeypatch.setattr(
        runtime._BaseKeyescapeEngine,
        "_remember_slot_timing",
        fake_base_remember,
    )
    state = {
        "boundary_fetch_started_ms": -250.0,
        "boundary_fetch_elapsed_ms": 670.0,
        "shared_wait_timeout_ms": 1000.0,
    }

    engine._remember_slot_timing(state)

    sample = state["timing_sample"]
    assert sample["observed_ready_delay_ms"] == pytest.approx(420.0)
    assert sample["boundary_fetch_started_ms"] == -250.0
    assert sample["boundary_fetch_elapsed_ms"] == 670.0
    assert sample["shared_wait_timeout_ms"] == 1000.0
