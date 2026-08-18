from __future__ import annotations

from types import SimpleNamespace

import engines.cgv_chrome_session as cgv_chrome_session
import ui.reservation_form_runtime as reservation_form_runtime
from engines.cgv_browser_client import CgvBrowserClient as BaseCgvBrowserClient
from engines.cgv_browser_client_runtime import CgvBrowserClient
from engines.cgv_engine_runtime import CgvEngine
from pengucro.models import ReservationRequest
from ui.cgv_booking_dialog_runtime import CgvBookingDialog
from ui.reservation_form_runtime import ReservationForm


def _engine(logs=None):
    logs = logs if logs is not None else []
    return CgvEngine(lambda message, level: logs.append((message, level)))


def test_open_target_date_drops_previous_date_preopen_templates(monkeypatch):
    exact = {
        "scnYmd": "20260824",
        "scnsNo": "target",
        "expoProdNm": "오디세이",
    }
    historical_template = {
        "scnYmd": "20260824",
        "scnsNo": "",
        "expoProdNm": "오디세이",
        "_pengucroPreopen": True,
        "_pengucroSeatReferenceDate": "2026-08-23",
    }

    monkeypatch.setattr(
        BaseCgvBrowserClient,
        "fetch_schedule_with_reference",
        lambda self, *args, **kwargs: (
            (exact, historical_template),
            "2026-08-24",
            False,
        ),
    )

    schedules, reference_date, reference_only = CgvBrowserClient().fetch_schedule_with_reference(
        "0013", "2026-08-24"
    )

    assert schedules == (exact,)
    assert reference_date == "2026-08-24"
    assert reference_only is False


def test_unopened_target_date_keeps_reference_template(monkeypatch):
    template = {
        "scnYmd": "20260824",
        "_pengucroPreopen": True,
        "_pengucroSeatReferenceDate": "2026-08-23",
    }
    monkeypatch.setattr(
        BaseCgvBrowserClient,
        "fetch_schedule_with_reference",
        lambda self, *args, **kwargs: ((template,), "2026-08-23", True),
    )

    schedules, reference_date, reference_only = CgvBrowserClient().fetch_schedule_with_reference(
        "0013", "2026-08-24"
    )

    assert schedules == (template,)
    assert reference_date == "2026-08-23"
    assert reference_only is True


def test_open_target_schedule_uses_its_own_seat_map_not_previous_day_reference():
    exact = {
        "scnYmd": "20260824",
        "scnsNo": "target-24",
        "_pengucroSeatReference": {
            "scnYmd": "20260823",
            "scnsNo": "reference-23",
        },
        "_pengucroSeatReferenceDate": "2026-08-23",
    }
    dialog = SimpleNamespace(selected_schedule=exact, schedules=(exact,))

    selected = CgvBookingDialog._seat_reference_schedule(dialog)

    assert selected["scnYmd"] == "20260824"
    assert selected["scnsNo"] == "target-24"


def test_preopen_schedule_still_uses_previous_day_reference_for_layout():
    reference = {"scnYmd": "20260823", "scnsNo": "reference-23"}
    template = {
        "scnYmd": "20260824",
        "scnsNo": "",
        "_pengucroPreopen": True,
        "_pengucroSeatReference": reference,
    }
    dialog = SimpleNamespace(selected_schedule=template, schedules=(template,))

    selected = CgvBookingDialog._seat_reference_schedule(dialog)

    assert selected == reference


def test_structured_seat_groups_survive_reservation_form_boundary(monkeypatch):
    request = ReservationRequest(
        site="CGV",
        branch="0013",
        reservation_date="2026-08-24",
        reservation_time="07:30:00",
        name="",
        phone="",
        people=2,
        theme_pk="오디세이",
        engine_metadata={"cgv": {"seats": "C8,C9 | D8,D9"}},
    )
    monkeypatch.setattr(
        reservation_form_runtime.BaseReservationForm,
        "get_reservation_data",
        lambda self: (request, "", 1, False),
    )

    form = ReservationForm.__new__(ReservationForm)
    form.cgv_selection = {"seat_groups": [["C8", "C9"], ["D8", "D9"]]}
    form._site_uses_cgv = lambda: True

    result, message, threads, is_async = ReservationForm.get_reservation_data(form)

    assert message == ""
    assert threads == 1
    assert is_async is False
    assert result.engine_metadata["cgv"]["seat_groups"] == [
        ["C8", "C9"],
        ["D8", "D9"],
    ]


def test_engine_prefers_structured_priority_order():
    serialized = CgvEngine._serialize_structured_seat_groups(
        [["C8", "C9"], ["C11", "C12"], ["D8", "D9"]],
        2,
    )
    assert serialized == "C8,C9 | C11,C12 | D8,D9"


def test_active_group_normalization_clears_stale_selection(monkeypatch):
    logs = []
    engine = _engine(logs)
    snapshots = iter(
        [
            {
                "ready": False,
                "extras": ["old-seat"],
                "missing": [],
                "selectedIds": ["old-seat", "11", "12"],
            },
            {
                "ready": True,
                "extras": [],
                "missing": [],
                "selectedIds": ["11", "12"],
            },
        ]
    )
    applied = []

    monkeypatch.setattr(
        engine,
        "_exact_seat_selection_snapshot",
        lambda _page, _ids: next(snapshots),
    )
    monkeypatch.setattr(
        engine,
        "_apply_exact_seat_selection",
        lambda _page, ids: applied.append(tuple(ids)) or True,
    )

    page = SimpleNamespace(wait_for_timeout=lambda _milliseconds: None)
    assert engine._normalize_active_seat_group(page, ["11", "12"]) is True
    assert applied == [("11", "12")]
    assert any("이전 선택 상태를 정리" in message for message, _level in logs)


def test_cgv_slot_manager_acquires_only_slot_one(monkeypatch):
    seen = {}

    class Lease:
        slot = 1
        port = 9333
        profile_path = "profile-1"

        @staticmethod
        def release():
            return None

    lease = Lease()
    fake_session = SimpleNamespace(
        port=9333,
        lease=lease,
        endpoint="http://127.0.0.1:9333",
    )

    def acquire(slot_count):
        seen["slot_count"] = slot_count
        return lease

    def start_or_attach(port, log, **kwargs):
        seen["port"] = port
        seen["profile_path"] = kwargs.get("profile_path")
        seen["allow_port_fallback"] = kwargs.get("allow_port_fallback")
        return fake_session

    monkeypatch.setattr(cgv_chrome_session.browser_session, "acquire_chrome_slot", acquire)
    monkeypatch.setattr(cgv_chrome_session.browser_session, "start_or_attach", start_or_attach)

    manager = cgv_chrome_session._CgvSlotOneManager()
    assert manager.start() is fake_session
    assert seen == {
        "slot_count": 1,
        "port": 9333,
        "profile_path": "profile-1",
        "allow_port_fallback": False,
    }
