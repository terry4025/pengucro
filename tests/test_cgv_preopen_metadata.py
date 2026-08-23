from types import SimpleNamespace

from ui.cgv_booking_dialog_preopen_auto import CgvBookingDialog


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def _confirm(schedule, *, reference_only: bool, reference_date: str):
    selected = []
    dialog = SimpleNamespace(
        selected_site=SimpleNamespace(
            site_no="0013", label="용산아이파크몰", region_code="01"
        ),
        priority_groups=[["H22", "H23"]],
        auto_seat_preference="",
        auto_seat_preference_label="",
        movie_var=Value("오디세이"),
        auditorium_var=Value("IMAX관 · IMAX LASER 2D"),
        preferred_times=["14:00"],
        reference_only=reference_only,
        selected_schedule=schedule,
        regions=[SimpleNamespace(code="01", name="서울")],
        reservation_date="2026-08-26",
        reference_date=reference_date,
        people=2,
        on_select=selected.append,
        _close_dialog=lambda: None,
    )

    CgvBookingDialog._confirm(dialog)

    return selected[0]


def test_preopen_confirm_preserves_nested_movie_and_exact_reference_date():
    result = _confirm(
        {
            "scnYmd": "20260826",
            "scnsNo": "",
            "_pengucroPreopen": True,
            "_pengucroSeatReference": {
                "movNo": "30001323",
                "scnYmd": "20260819",
                "scnsNo": "reference-1",
            },
        },
        reference_only=True,
        reference_date="2026-08-25",
    )

    assert result["is_preopen"] is True
    assert result["mov_no"] == "30001323"
    assert result["reference_date"] == "2026-08-19"


def test_real_selected_screening_metadata_wins_over_seat_reference():
    result = _confirm(
        {
            "movNo": "actual-movie",
            "scnYmd": "20260826",
            "scnsNo": "actual-screening",
            "_pengucroSeatReference": {
                "movNo": "reference-movie",
                "scnYmd": "20260819",
            },
        },
        reference_only=False,
        reference_date="2026-08-19",
    )

    assert result["is_preopen"] is False
    assert result["mov_no"] == "actual-movie"
    assert result["reference_date"] == "2026-08-26"
    assert result["scns_no"] == "actual-screening"
