from __future__ import annotations

import engines.cgv_engine as base_engine_module
import engines.cgv_engine_priority_ladder as ladder_module
from engines.cgv_engine_movie_identity_runtime import (
    CgvEngine,
    _PREOPEN_SELECTION_ACTIVE,
    select_schedule,
)
from engines.cgv_engine_priority_ladder_runtime import CgvEngine as PriorityRuntimeCgvEngine
from engines.cgv_movie_identity import strip_matching_format_suffix
from engines.cgv_preopen_matching import (
    context_matches,
    matching_schedule_candidates,
    rank_preopen_schedules,
)


def _schedule(
    time_text: str,
    *,
    auditorium: str = "IMAX관",
    format_name: str = "IMAX LASER 2D",
    movie: str = "오디세이",
    seq: str = "1",
    controlled: str = "N",
):
    return {
        "siteNo": "0013",
        "scnYmd": "20260826",
        "scnsNo": f"screen-{seq}",
        "scnSseq": seq,
        "scnsrtTm": time_text,
        "movNm": movie,
        "expoProdNm": movie,
        "expoScnsNm": auditorium,
        "scnsNm": auditorium,
        "movkndDsplEnm": format_name,
        "movkndDsplNm": format_name,
        "cntlYn": controlled,
    }


def _payload(*items):
    return {"data": list(items)}


def noop(*_args, **_kwargs):
    return None


def test_open_date_keeps_exact_time_semantics():
    payload = _payload(_schedule("1350"), _schedule("1730", seq="2"))
    chosen = select_schedule(
        payload,
        movie="오디세이",
        auditorium="IMAX관",
        format_name="IMAX LASER 2D",
        preferred_times=["14:00"],
    )
    assert chosen is None


def test_preopen_reference_time_maps_to_nearby_real_time():
    payload = _payload(_schedule("1350"), _schedule("1730", seq="2"))
    token = _PREOPEN_SELECTION_ACTIVE.set(True)
    try:
        chosen = select_schedule(
            payload,
            movie="오디세이",
            auditorium="IMAX관",
            format_name="IMAX LASER 2D",
            preferred_times=["14:00"],
        )
    finally:
        _PREOPEN_SELECTION_ACTIVE.reset(token)
    assert chosen is not None
    assert chosen["scnsrtTm"] == "1350"


def test_preopen_first_priority_near_match_beats_later_exact_match():
    candidates = [_schedule("1350"), _schedule("1730", seq="2")]
    ranked = rank_preopen_schedules(candidates, ["14:00", "17:30"])
    assert [item["scnsrtTm"] for item in ranked[:2]] == ["1350", "1730"]


def test_preopen_far_first_priority_does_not_steal_second_exact_match():
    candidates = [_schedule("1730")]
    ranked = rank_preopen_schedules(candidates, ["10:00", "17:30"])
    assert ranked[0]["scnsrtTm"] == "1730"


def test_preopen_additional_real_slots_remain_safety_fallbacks():
    candidates = [
        _schedule("1350", seq="1"),
        _schedule("1420", seq="2"),
        _schedule("1710", seq="3"),
    ]
    ranked = rank_preopen_schedules(candidates, ["14:00", "17:00"])
    assert [item["scnsrtTm"] for item in ranked] == ["1350", "1710", "1420"]


def test_imax_display_label_drift_is_tolerated_without_crossing_regular_2d():
    drifted_imax = _schedule(
        "1350",
        auditorium="IMAX",
        format_name="IMAX 2D",
    )
    regular = _schedule(
        "1400",
        auditorium="15관",
        format_name="2D",
        seq="2",
    )
    assert context_matches(drifted_imax, "IMAX관", "IMAX LASER 2D") is True
    assert context_matches(regular, "IMAX관", "IMAX LASER 2D") is False
    candidates = matching_schedule_candidates(
        _payload(drifted_imax, regular),
        movie="오디세이",
        auditorium="IMAX관",
        format_name="IMAX LASER 2D",
    )
    assert candidates == [drifted_imax]


def test_preopen_never_selects_controlled_schedule():
    locked = _schedule("1400", controlled="Y")
    token = _PREOPEN_SELECTION_ACTIVE.set(True)
    try:
        chosen = select_schedule(
            _payload(locked),
            movie="오디세이",
            auditorium="IMAX관",
            format_name="IMAX LASER 2D",
            preferred_times=["14:00"],
        )
    finally:
        _PREOPEN_SELECTION_ACTIVE.reset(token)
    assert chosen is None
    assert context_matches(
        locked,
        "IMAX관",
        "IMAX LASER 2D",
        include_controlled=True,
    ) is True


def test_missing_separate_format_still_normalizes_unmistakable_movie_suffix():
    assert strip_matching_format_suffix("오디세이(IMAX LASER 2D)", "") == "오디세이"
    assert strip_matching_format_suffix("제목(감독판)", "") == "제목(감독판)"


def test_final_runtime_patches_base_and_priority_ladder_selector():
    assert base_engine_module.select_schedule is select_schedule
    assert ladder_module.select_schedule is select_schedule


def test_make_reservation_thread_enables_preopen_context_only_for_scope(monkeypatch):
    observed = []

    def fake_make_reservation_thread(self, reservation_data):
        observed.append(_PREOPEN_SELECTION_ACTIVE.get())

    monkeypatch.setattr(
        PriorityRuntimeCgvEngine,
        "make_reservation_thread",
        fake_make_reservation_thread,
    )
    engine = CgvEngine(log_callback=noop, success_callback=noop)
    engine.make_reservation_thread(
        {"engine_metadata": {"cgv": {"is_preopen": True}}}
    )
    assert observed == [True]
    assert _PREOPEN_SELECTION_ACTIVE.get() is False
