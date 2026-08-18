import pytest

from engines.cgv_engine_pairwise import CgvEngine
from engines.cgv_engine_watchdog import CgvEngine as WatchdogCgvEngine
from engines.registry import EngineRegistry


def _labels(count: int) -> list[str]:
    return [f"B{10 + index}" for index in range(count)]


@pytest.mark.parametrize("people", range(1, 9))
def test_pairwise_click_plan_matches_cgv_two_plus_final_one_rule(people):
    target = _labels(people)
    expected = tuple(target[index] for index in range(0, people, 2))
    assert CgvEngine._pairwise_click_plan(target) == expected


def test_pairwise_prefix_states_for_odd_party():
    target = ["B10", "B11", "B12", "B13", "B14"]

    assert CgvEngine._pairwise_prefix_state(target, []) == ("advance", "B10", 2)
    assert CgvEngine._pairwise_prefix_state(target, ["B10", "B11"]) == (
        "advance",
        "B12",
        4,
    )
    assert CgvEngine._pairwise_prefix_state(
        target, ["B10", "B11", "B12", "B13"]
    ) == ("advance", "B14", 5)
    assert CgvEngine._pairwise_prefix_state(target, target) == (
        "complete",
        None,
        5,
    )


def test_half_pair_is_not_treated_as_a_stable_prefix():
    target = ["B10", "B11", "B12"]
    assert CgvEngine._pairwise_prefix_state(target, ["B10"])[0] == "invalid"


def test_duplicate_dom_representations_do_not_double_selected_count():
    snapshot = {
        "selectedIds": ["B10", "B11", "B10", "B11"],
        "submitReady": True,
    }
    normalized = CgvEngine._dedupe_snapshot(snapshot, ["B10", "B11"])

    assert normalized["selectedIds"] == ["B10", "B11"]
    assert normalized["missing"] == []
    assert normalized["extras"] == []
    assert normalized["ready"] is True


@pytest.mark.parametrize("people", range(1, 9))
def test_normalizer_clicks_only_front_anchor_for_each_cgv_pair(monkeypatch, people):
    engine = CgvEngine(lambda _message, _level: None)
    target = _labels(people)
    state = {"selected": [], "clicks": []}

    def snapshot(_page, target_ids):
        selected = list(state["selected"])
        target_set = set(target_ids)
        selected_set = set(selected)
        complete = selected == target_ids
        return {
            "selectedIds": selected,
            "extras": [value for value in selected if value not in target_set],
            "missing": [value for value in target_ids if value not in selected_set],
            "submitReady": complete,
            "ready": complete,
        }

    def click_anchor(_page, anchor):
        state["clicks"].append(anchor)
        selected_count = len(state["selected"])
        remaining = people - selected_count
        added = 2 if remaining >= 2 else 1
        state["selected"] = target[: selected_count + added]
        return True

    monkeypatch.setattr(engine, "_exact_seat_selection_snapshot", snapshot)
    monkeypatch.setattr(engine, "_click_pair_anchor", click_anchor)
    monkeypatch.setattr(engine, "_pairwise_wait", lambda _page: None)
    monkeypatch.setattr(engine, "_click_first_selected_seat", lambda _page: False)

    assert engine._normalize_active_seat_group(object(), target) is True
    assert state["selected"] == target
    assert state["clicks"] == list(CgvEngine._pairwise_click_plan(target))


def test_two_person_group_clicks_b10_once_not_b10_and_b11(monkeypatch):
    engine = CgvEngine(lambda _message, _level: None)
    target = ["B10", "B11"]
    state = {"selected": [], "clicks": []}

    def snapshot(_page, _target_ids):
        complete = state["selected"] == target
        return {
            "selectedIds": list(state["selected"]),
            "extras": [],
            "missing": [] if complete else list(target),
            "submitReady": complete,
            "ready": complete,
        }

    def click_anchor(_page, anchor):
        state["clicks"].append(anchor)
        state["selected"] = list(target)  # CGV selects B10+B11 from one B10 click.
        return True

    monkeypatch.setattr(engine, "_exact_seat_selection_snapshot", snapshot)
    monkeypatch.setattr(engine, "_click_pair_anchor", click_anchor)
    monkeypatch.setattr(engine, "_pairwise_wait", lambda _page: None)

    assert engine._normalize_active_seat_group(object(), target) is True
    assert state["clicks"] == ["B10"]


def test_stale_selection_is_cleared_before_pair_plan(monkeypatch):
    engine = CgvEngine(lambda _message, _level: None)
    target = ["B10", "B11", "B12"]
    state = {"selected": ["A1", "A2"], "anchors": [], "clears": 0}

    def snapshot(_page, target_ids):
        selected = list(state["selected"])
        target_set = set(target_ids)
        selected_set = set(selected)
        complete = selected == target
        return {
            "selectedIds": selected,
            "extras": [value for value in selected if value not in target_set],
            "missing": [value for value in target_ids if value not in selected_set],
            "submitReady": complete,
            "ready": complete,
        }

    def clear(_page):
        state["clears"] += 1
        state["selected"] = []
        return True

    def click_anchor(_page, anchor):
        state["anchors"].append(anchor)
        if anchor == "B10":
            state["selected"] = ["B10", "B11"]
        elif anchor == "B12":
            state["selected"] = list(target)
        return True

    monkeypatch.setattr(engine, "_exact_seat_selection_snapshot", snapshot)
    monkeypatch.setattr(engine, "_click_first_selected_seat", clear)
    monkeypatch.setattr(engine, "_click_pair_anchor", click_anchor)
    monkeypatch.setattr(engine, "_pairwise_wait", lambda _page: None)

    assert engine._normalize_active_seat_group(object(), target) is True
    assert state["clears"] == 1
    assert state["anchors"] == ["B10", "B12"]


def test_final_registry_runtime_includes_watchdog_and_pairwise_layers():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode="",
        payload={},
        custom_sites={},
        log_callback=lambda _message, _level: None,
        success_callback=None,
    )
    assert isinstance(engine, CgvEngine)
    assert isinstance(engine, WatchdogCgvEngine)
