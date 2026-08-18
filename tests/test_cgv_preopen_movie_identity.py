from __future__ import annotations

import engines.cgv_engine as base_engine_module
import engines.cgv_engine_priority_ladder as ladder_module
from engines.cgv_engine_movie_identity_runtime import (
    CgvEngine,
    _has_schedule_hint,
    select_schedule,
)
from engines.cgv_engine_priority_ladder_runtime import CgvEngine as PriorityLadderRuntimeCgvEngine
from engines.cgv_movie_identity import (
    schedule_matches_movie,
    schedule_movie_name,
    strip_matching_format_suffix,
)
from engines.registry import EngineRegistry
from pengucro.models import STANDARD_MODE


def _schedule(
    *,
    time_text: str = "1400",
    auditorium: str = "IMAX관",
    format_name: str = "IMAX LASER 2D",
    mov_name: str = "오디세이",
    expo_name: str = "",
    seq: str = "1",
):
    return {
        "siteNo": "0013",
        "scnYmd": "20260826",
        "scnsNo": "01",
        "scnSseq": seq,
        "scnsrtTm": time_text,
        "movNm": mov_name,
        "expoProdNm": expo_name,
        "expoScnsNm": auditorium,
        "scnsNm": auditorium,
        "movkndDsplEnm": format_name,
        "movkndDsplNm": format_name,
        "cntlYn": "N",
    }


def noop(*_args, **_kwargs):
    return None


def test_movie_display_prefers_clean_mov_name_over_expo_format_suffix():
    item = _schedule(expo_name="오디세이(IMAX LASER 2D)")
    assert schedule_movie_name(item) == "오디세이"


def test_format_suffix_is_removed_only_when_it_matches_separate_format():
    assert (
        strip_matching_format_suffix("오디세이(IMAX LASER 2D)", "IMAX LASER 2D")
        == "오디세이"
    )
    assert strip_matching_format_suffix("제목(감독판)", "IMAX LASER 2D") == "제목(감독판)"


def test_preopen_long_movie_name_matches_real_open_movie_name():
    actual = _schedule(
        mov_name="오디세이",
        expo_name="",
        auditorium="IMAX관",
        format_name="IMAX LASER 2D",
    )
    payload = {"data": [actual]}

    chosen = select_schedule(
        payload,
        movie="오디세이(IMAX LASER 2D)",
        auditorium="IMAX관",
        format_name="IMAX LASER 2D",
        preferred_times=["14:00"],
    )

    assert chosen == actual
    assert schedule_matches_movie(actual, "오디세이(IMAX LASER 2D)", "IMAX LASER 2D")


def test_preopen_hint_is_movie_only_even_when_regular_2d_appears_first():
    regular_2d = _schedule(
        auditorium="2관",
        format_name="2D",
        mov_name="오디세이",
        expo_name="",
    )
    payload = {"data": [regular_2d]}

    assert _has_schedule_hint(payload, "오디세이", "IMAX관") is True
    assert _has_schedule_hint(payload, "오디세이(IMAX LASER 2D)", "IMAX관") is True
    assert _has_schedule_hint(payload, "전혀 다른 영화", "IMAX관") is False


def test_canonical_fallback_never_crosses_into_regular_2d_screening():
    regular = _schedule(
        auditorium="15관",
        format_name="2D",
        mov_name="오디세이",
        seq="1",
    )
    imax = _schedule(
        auditorium="IMAX관",
        format_name="IMAX LASER 2D",
        mov_name="오디세이",
        seq="2",
    )
    payload = {"data": [regular, imax]}

    chosen = select_schedule(
        payload,
        movie="오디세이(IMAX LASER 2D)",
        auditorium="IMAX관",
        format_name="IMAX LASER 2D",
        preferred_times=["14:00"],
    )

    assert chosen == imax
    assert chosen["expoScnsNm"] == "IMAX관"
    assert chosen["movkndDsplEnm"] == "IMAX LASER 2D"


def test_final_runtime_patches_both_initial_detector_and_time_ladder():
    assert base_engine_module.select_schedule is select_schedule
    assert base_engine_module._has_schedule_hint is _has_schedule_hint
    assert ladder_module.select_schedule is select_schedule


def test_registry_keeps_priority_runtime_under_movie_identity_layer():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode=STANDARD_MODE,
        payload={},
        custom_sites={},
        log_callback=noop,
        success_callback=noop,
    )

    assert isinstance(engine, CgvEngine)
    assert isinstance(engine, PriorityLadderRuntimeCgvEngine)


def test_selector_and_reference_aggregator_share_clean_movie_name():
    import engines.cgv_browser_client as browser_client_module
    import ui.cgv_booking_dialog as dialog_module
    import ui.cgv_booking_dialog_movie_runtime  # noqa: F401

    item = _schedule(expo_name="오디세이(IMAX LASER 2D)")
    assert dialog_module._movie_name(item) == "오디세이"
    assert browser_client_module._movie_name(item) == "오디세이"
