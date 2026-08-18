from engines.cgv_engine_pairwise import CgvEngine as AdaptiveCgvEngine
from engines.cgv_engine_pairwise_observer import CgvEngine
from engines.registry import EngineRegistry


def test_observer_layer_uses_short_event_driven_fallback_ceilings():
    assert CgvEngine.PAIR_ACTION_SETTLE_SECONDS <= 0.22
    assert CgvEngine.PAIR_CLEAR_SETTLE_SECONDS <= 0.28
    assert CgvEngine.PAIR_IDLE_FALLBACK_MS <= 8


def test_wait_for_selection_change_accepts_observer_snapshot_without_python_polling(monkeypatch):
    engine = CgvEngine(lambda _message, _level="info": None)

    class Page:
        def __init__(self):
            self.calls = []

        def evaluate(self, script, payload):
            self.calls.append((script, payload))
            assert payload["observerKey"] == "__pengucroSeatSelectionObserverV2"
            return {
                "selectedIds": ["B19", "B20"],
                "submitReady": False,
                "selectedKey": '["B19","B20"]',
                "observerImmediate": True,
            }

    page = Page()
    monkeypatch.setattr(
        AdaptiveCgvEngine,
        "_wait_for_selection_change",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("polling fallback should not run")
        ),
    )

    snapshot = engine._wait_for_selection_change(
        page,
        ["B18", "B19", "B20"],
        set(),
        0.22,
    )

    assert snapshot["selectedIds"] == ["B19", "B20"]
    assert snapshot["missing"] == ["B18"]
    assert snapshot["extras"] == []


def test_normalizer_installs_observer_before_adaptive_state_machine_and_tears_down(monkeypatch):
    engine = CgvEngine(lambda _message, _level="info": None)
    events = []

    monkeypatch.setattr(
        engine,
        "_install_selection_observer",
        lambda _page: events.append("install") or True,
    )
    monkeypatch.setattr(
        engine,
        "_teardown_selection_observer",
        lambda _page: events.append("teardown"),
    )
    monkeypatch.setattr(
        AdaptiveCgvEngine,
        "_normalize_active_seat_group",
        lambda _self, _page, _seats: events.append("adaptive") or True,
    )

    assert engine._normalize_active_seat_group(object(), ["B18", "B19", "B20"]) is True
    assert events == ["install", "adaptive", "teardown"]


def test_final_registry_uses_observer_accelerated_adaptive_layer():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode="",
        payload={},
        custom_sites={},
        log_callback=lambda _message, _level="info": None,
        success_callback=None,
    )
    assert isinstance(engine, CgvEngine)
    assert isinstance(engine, AdaptiveCgvEngine)
