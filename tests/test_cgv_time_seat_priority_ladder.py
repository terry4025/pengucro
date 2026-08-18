from dataclasses import dataclass
from types import SimpleNamespace

from engines.cgv_client import CgvSeatGroup
from engines.cgv_engine_priority_ladder import CgvEngine
from engines.cgv_engine_visitor_dom_runtime import CgvEngine as VisitorDomCgvEngine
from ui.cgv_booking_dialog_preopen_auto import CgvBookingDialog
from ui.reservation_form_runtime import (
    BaseReservationForm,
    ReservationForm as RuntimeReservationForm,
)


def _schedule(time_text: str, seq: str, *, auditorium="IMAX관", format_name="IMAX LASER 2D"):
    return {
        "siteNo": "0013",
        "scnYmd": "20260826",
        "scnsNo": "01",
        "scnSseq": seq,
        "scnsrtTm": time_text,
        "movNm": "오디세이",
        "expoProdNm": "오디세이",
        "expoScnsNm": auditorium,
        "scnsNm": auditorium,
        "movkndDsplEnm": format_name,
        "movkndDsplNm": format_name,
    }


def test_ordered_candidates_keep_user_time_order_and_never_mix_regular_2d():
    engine = CgvEngine(lambda *_args: None)
    engine._priority_movie = "오디세이"
    engine._priority_auditorium = "IMAX관"
    engine._priority_format = "IMAX LASER 2D"
    engine._priority_preferred_times = ["14:00", "17:30", "10:30"]

    imax_1400 = _schedule("1400", "1")
    imax_1730 = _schedule("1730", "2")
    imax_1030 = _schedule("1030", "3")
    regular_1430 = _schedule(
        "1430", "4", auditorium="15관", format_name="2D"
    )
    engine._priority_schedule_payload = {
        "data": [regular_1430, imax_1030, imax_1730, imax_1400]
    }

    candidates = engine._ordered_schedule_candidates(imax_1400)

    assert [item["scnsrtTm"] for item in candidates] == ["1400", "1730", "1030"]
    assert all(item["expoScnsNm"] == "IMAX관" for item in candidates)
    assert all(item["movkndDsplEnm"] == "IMAX LASER 2D" for item in candidates)


def test_manual_seat_priority_is_checked_in_order():
    engine = CgvEngine(lambda *_args: None)
    engine._priority_manual_groups = (
        CgvSeatGroup(("C8", "C9", "C10")),
        CgvSeatGroup(("B8", "B9", "B10")),
        CgvSeatGroup(("F8", "F9", "F10")),
    )
    payload = {
        "data": {
            "items": [
                {
                    "seats": [
                        {
                            "seatLocNo": f"{row}-{number}",
                            "seatRowNm": row,
                            "seatNo": str(number),
                            "seatStusCd": (
                                "00" if row == "B" or (row == "C" and number != 9) else "01"
                            ),
                            "seatSaleYn": "Y",
                        }
                        for row in ("B", "C", "F")
                        for number in (8, 9, 10)
                    ]
                }
            ]
        }
    }

    chosen = engine._choose_priority_group(payload, _schedule("1400", "1"), 3)

    assert chosen is not None
    assert chosen.seats == ("B8", "B9", "B10")


def test_time_one_without_target_seats_falls_through_to_time_two(monkeypatch):
    engine = CgvEngine(lambda *_args: None)
    first = _schedule("1400", "1")
    second = _schedule("1730", "2")
    third = _schedule("1030", "3")
    engine._priority_movie = "오디세이"
    engine._priority_auditorium = "IMAX관"
    engine._priority_format = "IMAX LASER 2D"
    engine._priority_preferred_times = ["14:00", "17:30", "10:30"]
    engine._priority_manual_groups = (CgvSeatGroup(("C8", "C9", "C10")),)
    engine._priority_schedule_payload = {"data": [first, second, third]}
    engine._priority_last_schedule_refresh = 10**9
    engine._priority_primary_key = engine._schedule_key(first)

    inspected = []

    def read_once(_page, schedule, _people, *, allow_initial):
        inspected.append((schedule["scnsrtTm"], allow_initial))
        if schedule["scnsrtTm"] == "1730":
            return CgvSeatGroup(("C8", "C9", "C10")), {"statusCode": 0}, 200
        return None, {"statusCode": 0}, 200

    engine._read_schedule_once = read_once
    engine._refresh_priority_schedule_payload = lambda _page: None
    engine._activate_priority_schedule = lambda _page, _schedule, _people: True

    delegated = {}

    def delegated_hold(_self, _page, schedule, groups, _people, _dev, _cgv):
        delegated["time"] = schedule["scnsrtTm"]
        delegated["groups"] = groups
        return True, False

    monkeypatch.setattr(VisitorDomCgvEngine, "_watch_and_hold_api", delegated_hold)

    result = engine._watch_and_hold_api(
        object(),
        first,
        engine._priority_manual_groups,
        3,
        False,
        {},
    )

    assert result == (True, False)
    assert [value[0] for value in inspected] == ["1400", "1730"]
    assert delegated["time"] == "1730"
    assert delegated["groups"][0].seats == ("C8", "C9", "C10")


def test_preopen_auto_mode_can_enable_confirm_without_concrete_seats():
    class Value:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    class Button:
        def __init__(self):
            self.state = None

        def configure(self, **kwargs):
            self.state = kwargs.get("state", self.state)

    dialog = SimpleNamespace(
        movie_var=Value("오디세이"),
        auditorium_var=Value("IMAX관 · IMAX LASER 2D"),
        selected_site=object(),
        selected_schedule=_schedule("1400", "1"),
        preferred_times=["14:00", "17:30"],
        priority_groups=[],
        auto_seat_preference="best",
        confirm_button=Button(),
    )

    CgvBookingDialog._update_confirm_state(dialog)

    assert dialog.confirm_button.state == "normal"


def test_auto_only_form_placeholder_never_escapes_request(monkeypatch):
    @dataclass(frozen=True)
    class Request:
        engine_metadata: dict

    selection = {
        "people": 3,
        "seats": "",
        "seat_groups": [],
        "auto_seat_mode": "best",
        "auto_seat_label": "최우선 중앙 명당 3석",
    }
    form = SimpleNamespace(
        cgv_selection=selection,
        people_entry=SimpleNamespace(get=lambda: "3"),
        _site_uses_cgv=lambda: True,
    )

    def base_get_reservation_data(self):
        assert self.cgv_selection["seats"] == "A1,A2,A3"
        return Request({"cgv": {"seats": self.cgv_selection["seats"]}}), None, 2, True

    monkeypatch.setattr(BaseReservationForm, "get_reservation_data", base_get_reservation_data)

    request, message, threads, is_async = RuntimeReservationForm.get_reservation_data(form)

    assert message is None
    assert threads == 2
    assert is_async is True
    assert form.cgv_selection is selection
    assert request.engine_metadata["cgv"]["seats"] == ""
    assert request.engine_metadata["cgv"]["auto_seat_mode"] == "best"
    assert "seat_groups" not in request.engine_metadata["cgv"]
