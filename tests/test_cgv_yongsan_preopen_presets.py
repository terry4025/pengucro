from __future__ import annotations

from types import SimpleNamespace

import pytest

from engines.cgv_client import is_contiguous_seat_group
from engines.cgv_yongsan_preopen_presets import (
    YONGSAN_IMAX_CENTER,
    is_yongsan_imax_target,
    yongsan_imax_preopen_groups,
)
from ui.cgv_booking_dialog_movie_runtime import CgvBookingDialog as MovieIdentityDialog
from ui.cgv_booking_dialog_yongsan_preopen import CgvBookingDialog


class DummyControl:
    def __init__(self):
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)


class DummyVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _midpoint(group: tuple[str, ...]) -> float:
    numbers = [int("".join(ch for ch in seat if ch.isdigit())) for seat in group]
    return (numbers[0] + numbers[-1]) / 2.0


def test_yongsan_target_is_narrowly_scoped_to_site_0013_imax():
    assert is_yongsan_imax_target("0013", "IMAX관", "IMAX LASER 2D")
    assert not is_yongsan_imax_target("0013", "15관", "2D")
    assert not is_yongsan_imax_target("0142", "IMAX관", "IMAX LASER 2D")


@pytest.mark.parametrize("people", range(1, 9))
def test_balanced_preopen_groups_follow_people_count_and_center(people):
    groups = yongsan_imax_preopen_groups("balanced", people)

    assert groups
    assert all(len(group) == people for group in groups)
    assert all(is_contiguous_seat_group(group, people) for group in groups)
    assert all(seat.startswith("H") for group in groups for seat in group)
    assert abs(_midpoint(groups[0]) - YONGSAN_IMAX_CENTER) <= 0.5


def test_four_person_hardcoded_first_choices_match_dedicated_guide_rows():
    assert yongsan_imax_preopen_groups("balanced", 4)[0] == (
        "H21", "H22", "H23", "H24"
    )
    assert yongsan_imax_preopen_groups("immersive", 4)[0] == (
        "F21", "F22", "F23", "F24"
    )
    assert yongsan_imax_preopen_groups("comfortable", 4)[0] == (
        "I21", "I22", "I23", "I24"
    )
    assert yongsan_imax_preopen_groups("best", 4)[0] == (
        "H21", "H22", "H23", "H24"
    )


def test_odd_party_keeps_both_equally_centered_variants():
    groups = yongsan_imax_preopen_groups("balanced", 3)
    assert groups[0] == ("H21", "H22", "H23")
    assert groups[1] == ("H22", "H23", "H24")


def test_preopen_dropdown_stages_concrete_group_and_enables_visible_add_button():
    add_button = DummyControl()
    status_label = DummyControl()
    confirm_button = DummyControl()
    auto_var = DummyVar("명당 자동 선택")
    label = "몰입형 · F–G열 중앙 4석"

    dialog = SimpleNamespace(
        auto_seat_modes={"명당 자동 선택": "", label: "immersive"},
        auto_seat_preference="",
        auto_seat_preference_label="",
        seats=(),
        current_seats=set(),
        priority_groups=[],
        people=4,
        selected_site=SimpleNamespace(site_no="0013"),
        auditorium_var=DummyVar("IMAX관 · IMAX LASER 2D"),
        movie_var=DummyVar("오디세이"),
        preferred_times=["07:00"],
        selected_schedule=SimpleNamespace(),
        add_priority_button=add_button,
        status_label=status_label,
        confirm_button=confirm_button,
        auto_seat_var=auto_var,
    )

    CgvBookingDialog._auto_select_seats(dialog, label)

    assert dialog.auto_seat_preference == "immersive"
    assert dialog.current_seats == {"F21", "F22", "F23", "F24"}
    assert add_button.options["state"] == "normal"
    assert add_button.options["text"] == "명당 우선순위 추가"
    assert "F21, F22, F23, F24" in status_label.options["text"]


def test_final_selector_still_inherits_movie_identity_runtime():
    assert issubclass(CgvBookingDialog, MovieIdentityDialog)
