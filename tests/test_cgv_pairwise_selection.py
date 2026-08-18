import pytest

from engines.cgv_engine_pairwise import CgvEngine
from engines.cgv_engine_watchdog import CgvEngine as WatchdogCgvEngine
from engines.registry import EngineRegistry


def _labels(count: int, start: int = 10) -> list[str]:
    return [f"B{start + index}" for index in range(count)]


def test_attach_group_metadata_is_only_used_for_explicit_two_seat_groups():
    payload = {
        "data": {
            "items": [
                {
                    "seats": [
                        {"seatLocNo": "B17", "attachGroupNo": "G1"},
                        {"seatLocNo": "B18", "attachGroupNo": "G1"},
                        {"seatLocNo": "B19", "attachGroupNo": ""},
                        {"seatLocNo": "B20", "attachGroupNo": "G2"},
                        {"seatLocNo": "B21", "attachGroupNo": "G2"},
                        {"seatLocNo": "B22", "attachGroupNo": "G2"},
                    ]
                }
            ]
        }
    }

    assert CgvEngine._extract_attach_pairs(payload) == (("B17", "B18"),)


def test_three_person_user_case_prefers_known_b19_b20_pair():
    target = ["B18", "B19", "B20"]
    candidates = CgvEngine._adaptive_anchor_candidates(
        target,
        [],
        attach_pairs=(("B17", "B18"), ("B19", "B20")),
    )
    assert candidates[0] == "B19"


def test_three_person_without_metadata_uses_safe_interior_anchor_first():
    target = ["B18", "B19", "B20"]
    assert CgvEngine._adaptive_anchor_candidates(target, [])[0] == "B19"


@pytest.mark.parametrize("partner_direction", ["left", "right"])
def test_three_person_dynamic_partner_direction_succeeds(monkeypatch, partner_direction):
    engine = CgvEngine(lambda _message, _level: None)
    target = ["B18", "B19", "B20"]
    state = {"selected": [], "clicks": []}

    def snapshot(_page, target_ids):
        selected = list(state["selected"])
        selected_set = set(selected)
        target_set = set(target_ids)
        complete = selected_set == target_set and len(selected) == len(target)
        return {
            "selectedIds": selected,
            "extras": [value for value in selected if value not in target_set],
            "missing": [value for value in target_ids if value not in selected_set],
            "submitReady": complete,
            "ready": complete,
        }

    def click(_page, anchor):
        state["clicks"].append(anchor)
        selected = set(state["selected"])
        missing = set(target) - selected
        if len(missing) == 1:
            selected.add(anchor)
        elif anchor == "B19":
            if partner_direction == "left":
                selected.update(("B18", "B19"))
            else:
                selected.update(("B19", "B20"))
        else:
            selected.add(anchor)
        state["selected"] = [seat for seat in target if seat in selected]
        return True

    monkeypatch.setattr(engine, "_exact_seat_selection_snapshot", snapshot)
    monkeypatch.setattr(engine, "_click_pair_anchor", click)
    monkeypatch.setattr(engine, "_pairwise_wait", lambda _page: None)

    assert engine._normalize_active_seat_group(object(), target) is True
    assert set(state["selected"]) == set(target)
    assert state["clicks"][0] == "B19"
    assert len(state["clicks"]) == 2


def test_wrong_direction_pair_is_rolled_back_then_other_anchor_is_used(monkeypatch):
    engine = CgvEngine(lambda _message, _level: None)
    target = ["B18", "B19"]
    state = {"selected": [], "clicks": []}

    def snapshot(_page, target_ids):
        selected = list(state["selected"])
        selected_set = set(selected)
        target_set = set(target_ids)
        complete = selected_set == target_set and len(selected) == len(target)
        return {
            "selectedIds": selected,
            "extras": [value for value in selected if value not in target_set],
            "missing": [value for value in target_ids if value not in selected_set],
            "submitReady": complete,
            "ready": complete,
        }

    def click(_page, anchor):
        state["clicks"].append(anchor)
        current = set(state["selected"])
        # First B18 probe goes outward to B17+B18. Clicking B18 again reverses it.
        if anchor == "B18":
            if current == {"B17", "B18"}:
                current.clear()
            else:
                current = {"B17", "B18"}
        elif anchor == "B19":
            current = {"B18", "B19"}
        state["selected"] = sorted(current)
        return True

    monkeypatch.setattr(engine, "_exact_seat_selection_snapshot", snapshot)
    monkeypatch.setattr(engine, "_click_pair_anchor", click)
    monkeypatch.setattr(engine, "_pairwise_wait", lambda _page: None)

    assert engine._normalize_active_seat_group(object(), target) is True
    assert set(state["selected"]) == set(target)
    assert state["clicks"][:3] == ["B18", "B18", "B19"]


@pytest.mark.parametrize("people", range(1, 9))
def test_adaptive_normalizer_supports_one_to_eight_with_real_pair_groups(monkeypatch, people):
    engine = CgvEngine(lambda _message, _level: None)
    target = _labels(people)
    state = {"selected": set(), "clicks": []}

    # Simulate CGV pair groups B10+B11, B12+B13, ... . If only one visitor
    # slot remains, CGV accepts the final single seat.
    partner = {}
    for index in range(0, 10, 2):
        left = f"B{10 + index}"
        right = f"B{11 + index}"
        partner[left] = right
        partner[right] = left

    def snapshot(_page, target_ids):
        selected = [seat for seat in target_ids if seat in state["selected"]]
        selected_set = set(selected)
        target_set = set(target_ids)
        complete = selected_set == target_set and len(selected) == people
        return {
            "selectedIds": selected,
            "extras": [value for value in state["selected"] if value not in target_set],
            "missing": [value for value in target_ids if value not in selected_set],
            "submitReady": complete,
            "ready": complete,
        }

    def click(_page, anchor):
        state["clicks"].append(anchor)
        current = set(state["selected"])
        if anchor in current:
            mate = partner.get(anchor)
            current.discard(anchor)
            if mate:
                current.discard(mate)
        else:
            remaining = people - len(current)
            current.add(anchor)
            mate = partner.get(anchor)
            if remaining >= 2 and mate and mate not in current:
                current.add(mate)
        state["selected"] = current
        return True

    monkeypatch.setattr(engine, "_exact_seat_selection_snapshot", snapshot)
    monkeypatch.setattr(engine, "_click_pair_anchor", click)
    monkeypatch.setattr(engine, "_pairwise_wait", lambda _page: None)

    assert engine._normalize_active_seat_group(object(), target) is True
    assert state["selected"] == set(target)


def test_exact_target_waits_for_submit_ready_without_more_seat_clicks(monkeypatch):
    engine = CgvEngine(lambda _message, _level: None)
    target = ["B18", "B19", "B20"]
    calls = {"snapshot": 0, "click": 0}

    def snapshot(_page, target_ids):
        calls["snapshot"] += 1
        ready = calls["snapshot"] >= 3
        return {
            "selectedIds": list(target),
            "extras": [],
            "missing": [],
            "submitReady": ready,
            "ready": ready,
        }

    def click(_page, _anchor):
        calls["click"] += 1
        return True

    monkeypatch.setattr(engine, "_exact_seat_selection_snapshot", snapshot)
    monkeypatch.setattr(engine, "_click_pair_anchor", click)
    monkeypatch.setattr(engine, "_pairwise_wait", lambda _page: None)

    assert engine._normalize_active_seat_group(object(), target) is True
    assert calls["click"] == 0


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


def test_api_payload_attach_pairs_are_available_only_during_ui_sync(monkeypatch):
    engine = CgvEngine(lambda _message, _level: None)
    payload = {
        "data": {
            "items": [
                {
                    "seats": [
                        {"seatLocNo": "B19", "attachGroupNo": "PAIR1"},
                        {"seatLocNo": "B20", "attachGroupNo": "PAIR1"},
                    ]
                }
            ]
        }
    }
    observed = []

    def parent_sync(_self, _page, _payload, _selected):
        observed.append(tuple(getattr(engine, "_active_attach_pairs", ())))
        return True

    monkeypatch.setattr(
        WatchdogCgvEngine,
        "_select_api_seats_in_ui",
        parent_sync,
    )

    assert engine._select_api_seats_in_ui(object(), payload, []) is True
    assert observed == [(("B19", "B20"),)]
    assert getattr(engine, "_active_attach_pairs", ()) == ()


def test_final_registry_runtime_includes_watchdog_and_adaptive_pair_layer():
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
