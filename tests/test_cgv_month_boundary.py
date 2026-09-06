from types import SimpleNamespace

import pytest

from engines.cgv_browser_client import CgvBrowserClient
from engines.cgv_engine_movie_identity_runtime import (
    _PREOPEN_MOV_NO,
    _PREOPEN_SELECTION_ACTIVE,
    _PREOPEN_TIME_DRIFT,
    select_schedule,
)
from engines.cgv_engine_preopen_live_runtime import CgvEngine
from ui.cgv_booking_dialog import CgvBookingDialog


def _engine():
    return CgvEngine(lambda *_args: None)


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def _schedule(screening_date: str, time_text: str, *, seq: str = "1"):
    return {
        "siteNo": "0257",
        "scnYmd": screening_date,
        "scnsNo": f"screen-{seq}",
        "scnSseq": seq,
        "scnsrtTm": time_text,
        "movNo": "30001323",
        "movNm": "오디세이",
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "scnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "movkndDsplNm": "IMAX LASER 2D",
        "cntlYn": "N",
    }


class _BoundaryScheduleClient(CgvBrowserClient):
    def __init__(self, schedules_by_date):
        super().__init__()
        self.schedules_by_date = schedules_by_date
        self.requested_dates = []

    def _with_page(self, operation):
        return operation(object())

    def _fetch_schedule_on_page(self, _page, _site_no, date_digits, **_kwargs):
        self.requested_dates.append(date_digits)
        return tuple(self.schedules_by_date.get(date_digits, ()))


def test_preopen_reference_dates_cross_month_boundary_without_manual_math():
    engine = _engine()
    engine._preopen_sentinel_reference_date = "20260831"

    assert engine._reference_dates("20260901") == (
        "20260831",
        "20260830",
        "20260825",
    )


def test_preopen_reference_dates_cross_year_boundary_without_manual_math():
    engine = _engine()
    engine._preopen_sentinel_reference_date = "20261231"

    assert engine._reference_dates("20270101") == (
        "20261231",
        "20261230",
        "20261225",
    )


@pytest.mark.parametrize(
    ("formatted_date", "date_digits"),
    (("2026-09-01", "20260901"), ("2027-01-01", "20270101")),
)
def test_schedule_url_preserves_first_day_of_new_period(
    formatted_date, date_digits
):
    assert f"scnYmd={date_digits}" in CgvEngine._schedule_url(
        "0257", formatted_date
    )


def test_dialog_next_date_crosses_month_and_year_boundaries():
    observed = []
    dialog = SimpleNamespace(
        reservation_date="2026-08-31",
        _change_date=observed.append,
    )

    CgvBookingDialog._next_date(dialog)
    assert observed == ["2026-09-01"]

    observed.clear()
    dialog.reservation_date = "2026-12-31"
    CgvBookingDialog._next_date(dialog)
    assert observed == ["2027-01-01"]


@pytest.mark.parametrize(
    ("target_date", "previous_date"),
    (("20260901", "20260831"), ("20270101", "20261231")),
)
def test_reference_schedule_fetch_crosses_calendar_boundaries(
    target_date, previous_date
):
    reference = _schedule(previous_date, "1400")
    client = _BoundaryScheduleClient({previous_date: (reference,)})

    schedules, reference_date, reference_only = client.fetch_schedule_with_reference(
        "0257", target_date, max_reference_days=1
    )

    assert client.requested_dates == [target_date, previous_date]
    assert reference_only is True
    assert reference_date == (
        f"{previous_date[:4]}-{previous_date[4:6]}-{previous_date[6:]}"
    )
    assert schedules[0]["scnYmd"] == target_date
    assert schedules[0]["_pengucroSeatReference"]["scnYmd"] == previous_date


@pytest.mark.parametrize(
    ("target_date", "previous_date"),
    (("20260901", "20260831"), ("20270101", "20261231")),
)
def test_date_sentinel_compares_complete_dates_across_boundaries(
    target_date, previous_date
):
    engine = _engine()
    engine._preopen_sentinel_date_listed = None

    engine._consume_date_sentinel_result(
        {
            "ok": True,
            "status": 200,
            "data": {"statusCode": 0, "data": [{"scnYmd": previous_date}]},
        },
        target_date=target_date,
        mov_no="30001323",
    )
    assert engine._preopen_sentinel_date_listed is False

    engine._consume_date_sentinel_result(
        {
            "ok": True,
            "status": 200,
            "data": {
                "statusCode": 0,
                "data": [
                    {"scnYmd": previous_date},
                    {"scnYmd": target_date},
                ],
            },
        },
        target_date=target_date,
        mov_no="30001323",
    )
    assert engine._preopen_sentinel_date_listed is True


@pytest.mark.parametrize("target_date", ("20260901", "20270101"))
def test_preferred_schedule_selection_preserves_new_period_date(target_date):
    earlier = _schedule(target_date, "1350", seq="1")
    later = _schedule(target_date, "1730", seq="2")
    active_token = _PREOPEN_SELECTION_ACTIVE.set(True)
    drift_token = _PREOPEN_TIME_DRIFT.set(15)
    movie_token = _PREOPEN_MOV_NO.set("30001323")
    try:
        chosen = select_schedule(
            {"data": [later, earlier]},
            movie="오디세이",
            auditorium="IMAX관",
            format_name="IMAX LASER 2D",
            preferred_times=["14:00", "17:30"],
        )
    finally:
        _PREOPEN_MOV_NO.reset(movie_token)
        _PREOPEN_TIME_DRIFT.reset(drift_token)
        _PREOPEN_SELECTION_ACTIVE.reset(active_token)

    assert chosen is not None
    assert chosen["scnYmd"] == target_date
    assert chosen["scnsrtTm"] == "1350"


def test_preopen_confirmation_preserves_target_and_reference_months():
    selected = []
    dialog = SimpleNamespace(
        selected_site=SimpleNamespace(
            site_no="0257", label="용산아이파크몰", region_code="01"
        ),
        priority_groups=[["H22", "H23"]],
        auto_seat_preference="",
        auto_seat_preference_label="",
        movie_var=_Value("오디세이"),
        auditorium_var=_Value("IMAX관 · IMAX LASER 2D"),
        preferred_times=["14:00"],
        reference_only=True,
        selected_schedule={
            "scnYmd": "20260901",
            "scnsNo": "",
            "_pengucroPreopen": True,
            "_pengucroSeatReference": {
                "movNo": "30001323",
                "scnYmd": "20260831",
                "scnsNo": "reference-1",
            },
        },
        regions=[SimpleNamespace(code="01", name="서울")],
        reservation_date="2026-09-01",
        reference_date="2026-08-31",
        people=2,
        on_select=selected.append,
        _close_dialog=lambda: None,
    )

    CgvBookingDialog._confirm(dialog)

    assert selected[0]["date"] == "2026-09-01"
    assert selected[0]["reference_date"] == "2026-08-31"
    assert selected[0]["mov_no"] == "30001323"
