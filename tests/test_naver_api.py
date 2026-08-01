"""Tests for the Naver GraphQL client and its availability predicate.

The predicate is the one inference this whole engine rests on, and it was pinned
down by lining the API up against the rendered page for 18 slots across two dates:

    page 매진  ->  stock 1, bookingCount 1
    page 가능  ->  stock 1, bookingCount 0

So a slot is free when ``bookingCount < stock``. Reading ``stock`` alone as
"remaining" passes a casual look -- sample a range while everything is booked and
every slot reads ``stock: 1`` -- and produces an engine that submits into 매진
slots forever, which is exactly what happened before these tests existed.
"""

from datetime import datetime, timedelta

import pytest

from engines.naver_api import (
    KST,
    NaverApiError,
    NaverBookingApi,
    NaverServerClock,
    NaverSlot,
    parse_ids,
    participant_option,
)


def slot_payload(**overrides):
    payload = {
        "id": "7094790_1305498597_1321964504_2026-07-26T09:50:00+09:00",
        "slotId": "1321964504",
        "scheduleId": "1305498597",
        "detailScheduleId": None,
        "unitStartDateTime": "2026-07-26T00:50:00Z",
        "unitStartTime": "2026-07-26 09:50:00",
        "stock": 1,
        "bookingCount": 0,
        "occupiedBookingCount": 0,
        "unitStock": 1,
        "unitBookingCount": 0,
        "isBusinessDay": True,
        "isSaleDay": True,
        "isUnitSaleDay": True,
        "isUnitBusinessDay": True,
        "isHoliday": False,
        "minBookingCount": 1,
        "maxBookingCount": 1,
        "saleStartDateTime": None,
        "saleEndDateTime": None,
    }
    payload.update(overrides)
    return payload


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.headers = {}

    def json(self):
        return self._body


def api_with(monkeypatch, body):
    api = NaverBookingApi("1498729", "7094790", "12")
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse(body)

    monkeypatch.setattr(api.session, "post", fake_post)
    return api, calls


# -- URL parsing ----------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://m.booking.naver.com/booking/12/bizes/1498729/items/7094790"
            "?area=bmp&lang=ko",
            ("12", "1498729", "7094790"),
        ),
        (
            "https://booking.naver.com/booking/12/bizes/1498729/items/7094790",
            ("12", "1498729", "7094790"),
        ),
        ("https://booking.naver.com/booking/12/bizes/1498729", None),
        ("https://zerogangnam.com/reservation", None),
        ("", None),
    ],
)
def test_parse_ids(url, expected):
    assert parse_ids(url) == expected


# -- the availability predicate -------------------------------------------
def test_free_slot_has_no_bookings():
    """stock 1 / bookingCount 0 -- what the page renders as 오후 1:20 1매."""
    slot = NaverSlot.from_payload(slot_payload(bookingCount=0, unitBookingCount=0))
    assert slot.time_str == "09:50"
    assert slot.date_str == "2026-07-26"
    assert slot.remaining == 1
    assert slot.blocked_reason() is None
    assert slot.is_open()


def test_booking_count_reaching_stock_is_the_taken_signal():
    """stock 1 / bookingCount 1 -- what the page renders as 오후 12:10 매진.

    This is the case the engine used to read as "one seat free".
    """
    slot = NaverSlot.from_payload(slot_payload(stock=1, bookingCount=1))
    assert slot.remaining == 0
    assert slot.blocked_reason() == "정원 마감"
    assert not slot.is_open()


def test_zero_stock_slot_is_blocked():
    # 2026-08-08 18:00 on the sample item: capacity removed by the owner.
    slot = NaverSlot.from_payload(slot_payload(stock=0, unitStock=0, bookingCount=1))
    assert slot.remaining == 0
    assert slot.blocked_reason() == "정원 마감"


def test_partially_booked_multi_seat_slot_stays_open():
    slot = NaverSlot.from_payload(slot_payload(stock=4, bookingCount=3))
    assert slot.remaining == 1
    assert slot.is_open()


def test_occupied_holds_are_subtracted():
    slot = NaverSlot.from_payload(
        slot_payload(stock=2, bookingCount=0, occupiedBookingCount=2))
    assert slot.remaining == 0
    assert not slot.is_open()


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"isBusinessDay": False}, "휴무일"),
        ({"isUnitBusinessDay": False}, "휴무일"),
        ({"isSaleDay": False}, "판매 중지"),
        ({"isUnitSaleDay": False}, "판매 중지"),
    ],
)
def test_site_flags_close_a_slot(overrides, reason):
    assert NaverSlot.from_payload(slot_payload(**overrides)).blocked_reason() == reason


def test_sale_window_is_respected_when_present():
    now = datetime(2026, 7, 26, 10, 0, tzinfo=KST)
    early = NaverSlot.from_payload(
        slot_payload(saleStartDateTime="2026-07-26T12:00:00+09:00"))
    assert "판매 시작 전" in (early.blocked_reason(now) or "")

    late = NaverSlot.from_payload(
        slot_payload(saleEndDateTime="2026-07-26T09:00:00+09:00"))
    assert late.blocked_reason(now) == "판매 종료"

    inside = NaverSlot.from_payload(slot_payload(
        saleStartDateTime="2026-07-26T09:00:00+09:00",
        saleEndDateTime="2026-07-26T23:00:00+09:00"))
    assert inside.blocked_reason(now) is None


def test_holiday_flag_alone_does_not_block():
    # Escape rooms trade on holidays; only the business-day flags decide.
    assert NaverSlot.from_payload(slot_payload(isHoliday=True)).is_open()


# -- client behaviour -----------------------------------------------------
def test_fetch_slots_sorts_and_maps(monkeypatch):
    body = {"data": {"schedule": {"bizItemSchedule": {"hourly": [
        slot_payload(unitStartTime="2026-07-26 18:00:00", slotId="late"),
        slot_payload(unitStartTime="2026-07-26 09:50:00", slotId="early"),
    ]}}}}
    api, calls = api_with(monkeypatch, body)
    slots = api.fetch_slots("2026-07-26")

    assert [slot.slot_id for slot in slots] == ["early", "late"]
    params = calls[0]["json"]["variables"]["scheduleParams"]
    assert params["startDateTime"] == "2026-07-26T00:00:00"
    assert params["endDateTime"] == "2026-07-26T23:59:59"


def test_fetch_slot_raw_keeps_page_booking_fields(monkeypatch):
    body = {"data": {"schedule": {"bizItemSchedule": {"hourly": [
        slot_payload(
            name="",
            unitStartTime="2026-08-08 14:30:00",
            unitStartDateTime="2026-08-08T05:30:00Z",
            slotId="1331382668",
            duration=None,
            desc="",
            prices=[{
                "priceId": "8895079",
                "price": 33000,
                "name": "1인",
                "isImp": True,
            }],
        ),
    ]}}}}
    api, _ = api_with(monkeypatch, body)

    slot = api.fetch_slot_raw("2026-08-08", "14:30")

    assert slot["slotId"] == "1331382668"
    assert slot["unitStartDateTime"] == "2026-08-08T05:30:00Z"
    assert slot["prices"][0]["price"] == 33000


def test_find_slot_matches_on_hhmm(monkeypatch):
    body = {"data": {"schedule": {"bizItemSchedule": {"hourly": [
        slot_payload(unitStartTime="2026-07-26 09:50:00", slotId="a"),
        slot_payload(unitStartTime="2026-07-26 11:00:00", slotId="b"),
    ]}}}}
    api, _ = api_with(monkeypatch, body)
    assert api.find_slot("2026-07-26", "11:00").slot_id == "b"
    assert api.find_slot("2026-07-26", "11:00:00").slot_id == "b"
    assert api.find_slot("2026-07-26", "23:00") is None


def test_missing_date_yields_no_slots(monkeypatch):
    """A date outside the open window comes back empty -- the wait signal."""
    api, _ = api_with(
        monkeypatch, {"data": {"schedule": {"bizItemSchedule": {"hourly": []}}}})
    assert api.fetch_slots("2026-08-05") == []
    assert api.find_slot("2026-08-05", "18:00") is None


def test_graphql_errors_become_naver_api_error(monkeypatch):
    api, _ = api_with(monkeypatch, {"errors": [{"message": "Syntax Error"}]})
    with pytest.raises(NaverApiError):
        api.fetch_slots("2026-07-26")


def test_item_meta_reads_json_scalars(monkeypatch):
    body = {"data": {"bizItem": {
        "name": "사요나라, 세이코!",
        "currentDateTime": "2026-07-26T06:38:22.688Z",
        "isClosedBooking": False,
        "isClosedBookingUser": False,
        "bookableSettingJson": {
            "isPaused": False, "isUseOpen": True,
            "openDateTime": "2026-07-26T00:00:00+09:00", "isOpened": True,
        },
        "customFormJson": None,
    }}}
    api, _ = api_with(monkeypatch, body)
    meta = api.fetch_item_meta()

    assert meta.name == "사요나라, 세이코!"
    # 06:38:22Z is 15:38:22 KST.
    assert meta.server_time.strftime("%Y-%m-%d %H:%M:%S") == "2026-07-26 15:38:22"
    assert meta.server_time.microsecond == 688000
    assert meta.open_at.strftime("%H:%M") == "00:00"
    assert meta.uses_open_schedule and meta.is_opened
    assert meta.hard_block() is None


@pytest.mark.parametrize(
    "flags,expected",
    [
        ({"isClosedBooking": True}, "이 상품은 현재 예약이 닫혀 있습니다."),
        ({"isClosedBookingUser": True}, "이 상품은 현재 예약이 닫혀 있습니다."),
    ],
)
def test_hard_blocks(monkeypatch, flags, expected):
    item = {"name": "x", "currentDateTime": None, "bookableSettingJson": {}}
    item.update(flags)
    api, _ = api_with(monkeypatch, {"data": {"bizItem": item}})
    assert api.fetch_item_meta().hard_block() == expected


def test_paused_item_is_blocked(monkeypatch):
    api, _ = api_with(monkeypatch, {"data": {"bizItem": {
        "name": "x", "currentDateTime": None,
        "bookableSettingJson": {"isPaused": True},
    }}})
    assert api.fetch_item_meta().hard_block() == "판매가 일시 중지된 상품입니다."


# -- participant form ------------------------------------------------------
FORM = [{
    "type": "SELECT",
    "title": "참여인원 설정",
    "required": "y",
    "options": [{"idx": i, "value": f"{n}인"} for i, n in enumerate((2, 3, 4, 5))],
}]


@pytest.mark.parametrize(
    "people,expected",
    [
        ("3", ("참여인원 설정", "3인")),
        ("3인", ("참여인원 설정", "3인")),
        ("5", ("참여인원 설정", "5인")),
        ("9", None),
        ("", None),
    ],
)
def test_participant_option(people, expected):
    assert participant_option(FORM, people) == expected


def test_participant_option_ignores_non_select_questions():
    form = [{"type": "TEXT", "title": "요청사항", "options": [{"value": "3인"}]}]
    assert participant_option(form, "3") is None


# -- clock -----------------------------------------------------------------
def test_clock_anchors_to_monotonic(monkeypatch):
    body = {"data": {"bizItem": {
        "name": "x",
        "currentDateTime": "2026-07-26T06:38:22.688Z",
        "bookableSettingJson": {},
    }}}
    api, _ = api_with(monkeypatch, body)
    clock = NaverServerClock(api)

    assert not clock.synced
    assert clock.sync() is True
    assert clock.synced

    expected = datetime(2026, 7, 26, 15, 38, 22, 688000, tzinfo=KST)
    assert abs(clock.now() - expected.timestamp()) < 1.0
    # Precision is bounded by half the round trip, not by a whole second.
    assert clock.last_precision < 1.0

    target = expected + timedelta(minutes=10)
    assert 590 < clock.seconds_until(target.timestamp()) <= 600


def test_clock_reports_failure_without_raising(monkeypatch):
    api, _ = api_with(monkeypatch, {"data": {"bizItem": {
        "name": "x", "currentDateTime": None, "bookableSettingJson": {},
    }}})
    messages = []
    clock = NaverServerClock(api, log=lambda m, level="info": messages.append(m))

    assert clock.sync(announce=True) is False
    assert not clock.synced
    # Falls back to the local clock rather than blocking the run.
    assert clock.now() > 0
    assert any("서버 시간" in message for message in messages)


# -- direct submit_booking tests ------------------------------------------
def test_submit_booking_success(monkeypatch):
    from engines.naver_api import SubmitOutcome

    body = {"data": {"submitBooking": {"bookingId": "999888", "url": "https://m.booking.naver.com"}}}
    api, _ = api_with(monkeypatch, body)
    res = api.submit_booking({"businessId": "1498729"})

    assert res.outcome == SubmitOutcome.SUCCESS
    assert res.booking_id == "999888"


def test_submit_booking_refusal_rt77(monkeypatch):
    from engines.naver_api import SubmitOutcome

    body = {
        "errors": [
            {
                "message": "RT77",
                "extensions": {"code": "RT77", "reason": "이미 마감된 일정입니다."},
            }
        ]
    }
    api, _ = api_with(monkeypatch, body)
    res = api.submit_booking({"businessId": "1498729"})

    assert res.outcome == SubmitOutcome.REFUSED
    assert res.code == "RT77"
    assert "마감된 일정" in res.message


def test_submit_booking_not_open(monkeypatch):
    from engines.naver_api import SubmitOutcome

    body = {"errors": [{"message": "BizItem is not opened."}]}
    api, _ = api_with(monkeypatch, body)
    res = api.submit_booking({"businessId": "1498729"})

    assert res.outcome == SubmitOutcome.NOT_OPEN


def test_submit_booking_abuse_rt98(monkeypatch):
    from engines.naver_api import SubmitOutcome

    body = {
        "errors": [
            {
                "message": "RT98",
                "extensions": {"code": "RT98", "reason": "비정상 요청 탐지"},
            }
        ]
    }
    api, _ = api_with(monkeypatch, body)
    res = api.submit_booking({"businessId": "1498729"})

    assert res.outcome == SubmitOutcome.ABUSE


def test_specific_submit_reason_beats_generic_graphql_code():
    from engines.naver_api import SubmitOutcome, classify_submit_error

    assert (
        classify_submit_error("BAD_USER_INPUT", "RT98")
        == SubmitOutcome.ABUSE
    )
    assert (
        classify_submit_error(
            "BAD_USER_INPUT",
            "BizItem is not opened.",
        )
        == SubmitOutcome.NOT_OPEN
    )


def test_attach_cookies():
    api = NaverBookingApi("1498729", "7094790", "12")
    cookies = [
        {"name": "NID_AUT", "value": "secret_aut", "domain": ".naver.com"},
        {"name": "NID_SES", "value": "secret_ses", "domain": ".naver.com"},
        {"name": "OTHER", "value": "val", "domain": "example.com"},
    ]
    added = api.attach_cookies(cookies)
    assert added == 2
    assert api.session.cookies.get("NID_AUT") == "secret_aut"
