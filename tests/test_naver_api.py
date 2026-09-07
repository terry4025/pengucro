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
from types import SimpleNamespace

import pytest

from engines.naver_api import (
    KST,
    NaverApiError,
    NaverBookingApi,
    NaverItemMeta,
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


@pytest.mark.parametrize("operation", ["hourlySchedule", "Slot", "business", "bizItem"])
def test_public_reads_acquire_shared_budget(monkeypatch, operation):
    api, calls = api_with(monkeypatch, {"data": {"value": "public"}})
    permits = []
    cached = []
    stop_event = object()
    api.read_stop_event = stop_event
    api.read_coordinator = SimpleNamespace(
        acquire_read=lambda name, **kwargs: permits.append((name, kwargs)),
        get_public_read=lambda *args, **kwargs: None,
        put_public_read=lambda *args, **kwargs: cached.append((args, kwargs)),
    )

    result = api._post(operation, "query", {"item": "example"})

    assert len(calls) == 1
    assert permits[0][0] == operation
    assert permits[0][1]["stop_event"] is stop_event
    assert "deadline" in permits[0][1]
    assert result["value"] == "public"
    assert bool(cached) == (operation == "hourlySchedule")


def test_account_and_booking_mutation_never_use_public_read_sharing(monkeypatch):
    api, calls = api_with(monkeypatch, {"data": {"value": "private"}})

    def unexpected(*args, **kwargs):
        raise AssertionError("private operation entered public read coordination")

    api.read_coordinator = SimpleNamespace(
        acquire_read=unexpected, get_public_read=unexpected, put_public_read=unexpected,
    )
    api._post("account", "query", {})
    api._post_body("submitBooking", "mutation", {})
    assert len(calls) == 2


@pytest.mark.parametrize("after_wait", [False, True])
def test_shared_schedule_is_not_a_fresh_rtt_sample(monkeypatch, after_wait):
    api, calls = api_with(monkeypatch, {"data": {"unused": True}})
    cached = {"schedule": {"bizItemSchedule": {}}, "__rtt_window__": (1.0, 1.1)}
    cache_results = iter([None, cached] if after_wait else [cached])
    permits = []
    api.last_rtt = 0.200
    api.read_coordinator = SimpleNamespace(
        acquire_read=lambda *args, **kwargs: permits.append(args),
        get_public_read=lambda *args, **kwargs: next(cache_results),
    )

    result = api._post("hourlySchedule", "query", {"date": "2026-09-07"})

    assert calls == []
    assert len(permits) == int(after_wait)
    assert "__rtt_window__" not in result
    assert api.last_rtt is None
    assert api.last_read_from_cache is True


def test_server_clock_reads_always_go_to_network(monkeypatch):
    api, calls = api_with(monkeypatch, {"data": {"bizItem": {
        "currentDateTime": "2026-09-07T00:00:00.123Z",
    }}})
    permits = []

    def unexpected_cache(*args, **kwargs):
        raise AssertionError("server clock was cached")

    api.read_coordinator = SimpleNamespace(
        acquire_read=lambda *args, **kwargs: permits.append(args),
        get_public_read=unexpected_cache,
        put_public_read=unexpected_cache,
    )
    for _ in range(2):
        meta = api.fetch_item_meta()
        assert meta.request_started_monotonic <= meta.response_end_monotonic
    assert len(calls) == 2
    assert len(permits) == 2


def test_cancelled_public_read_wait_does_not_send_request(monkeypatch):
    import requests

    api, calls = api_with(monkeypatch, {"data": {"bizItem": {}}})

    def cancelled(*args, **kwargs):
        raise requests.RequestException("cancelled before network")

    api.read_coordinator = SimpleNamespace(acquire_read=cancelled)

    with pytest.raises(NaverApiError, match="공개 조회 대기 실패"):
        api.fetch_item_meta()
    assert calls == []


@pytest.mark.parametrize("method", ["fetch_slots_raw", "fetch_slots", "fetch_slot_raw", "find_slot"])
def test_fresh_schedule_methods_bypass_cache_but_share_network_result(monkeypatch, method):
    api, calls = api_with(monkeypatch, {"data": {
        "schedule": {"bizItemSchedule": {"hourly": [slot_payload()]}},
    }})
    permits, writes = [], []

    def stale_cache(*args, **kwargs):
        raise AssertionError("fresh read consulted a cached schedule")

    api.read_coordinator = SimpleNamespace(
        acquire_read=lambda *args, **kwargs: permits.append(args),
        get_public_read=stale_cache,
        put_public_read=lambda *args, **kwargs: writes.append(args),
    )
    arguments = ("2026-07-26", "09:50") if method in {"fetch_slot_raw", "find_slot"} else ("2026-07-26",)

    assert getattr(api, method)(*arguments, fresh=True) is not None

    assert len(calls) == len(permits) == len(writes) == 1
    assert api.last_read_from_cache is False


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


@pytest.mark.parametrize(
    "server_value,expected",
    [
        (None, False),
        (False, False),
        (True, True),
    ],
)
def test_selected_slot_payment_timing_matches_naver_page_semantics(
    monkeypatch, server_value, expected
):
    body = {"data": {"slotSeat": {"slot": {
        "id": "1303986499",
        "isPostPayment": server_value,
    }}}}
    api, calls = api_with(monkeypatch, body)

    assert api.fetch_slot_post_payment("1303986499") is expected
    variables = calls[0]["json"]["variables"]["slotSeatInput"]
    assert variables == {
        "businessId": "1498729",
        "bizItemId": "7094790",
        "slotId": "1303986499",
    }


def test_selected_slot_payment_timing_returns_unknown_for_incomplete_response(
    monkeypatch,
):
    api, _ = api_with(monkeypatch, {"data": {"slotSeat": None}})

    assert api.fetch_slot_post_payment("1303986499") is None


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


def test_target_open_time_is_shifted_from_latest_published_schedule(monkeypatch):
    """openDateTime belongs to the last published day, not every target day.

    Live example (오늘의 한 페이지 / 버디, 2026-08-02): the item reports
    2026-08-01 22:00 while schedules exist through 2026-08-08.  Therefore the
    still-closed 2026-08-09 date opens one day later, at 2026-08-02 22:00.
    """
    api = NaverBookingApi("1325520", "6446475", "12")
    meta = NaverItemMeta(
        name="버디",
        server_time=datetime(2026, 8, 2, 17, 51, tzinfo=KST),
        is_closed_booking=False,
        is_closed_for_user=False,
        open_at=datetime(2026, 8, 1, 22, 0, tzinfo=KST),
        is_opened=True,
        uses_open_schedule=True,
        is_paused=False,
        custom_form=[],
    )
    published = [
        NaverSlot.from_payload(slot_payload(unitStartTime="2026-08-02 09:40:00")),
        NaverSlot.from_payload(slot_payload(unitStartTime="2026-08-08 22:40:00")),
    ]
    monkeypatch.setattr(api, "fetch_slots", lambda *_args, **_kwargs: published)

    resolved = api.resolve_target_open_at("2026-08-09", meta)

    assert resolved == datetime(2026, 8, 2, 22, 0, tzinfo=KST)


def test_published_target_never_moves_before_announced_open_time(monkeypatch):
    """A target already returned by Naver uses the announced opening marker.

    Channel 27 exposed 2026-08-17 in its public schedule while also publishing
    later days through 2026-08-26.  Treating the latest day as the target's
    anchor moved the opening nine days into the past and caused an early submit.
    """
    api = NaverBookingApi("1498729", "7193259", "12")
    announced = datetime(2026, 8, 11, 0, 0, tzinfo=KST)
    meta = NaverItemMeta(
        name="붐붐박사의 폭죽놀이 유토피아",
        server_time=datetime(2026, 8, 10, 23, 57, tzinfo=KST),
        is_closed_booking=False,
        is_closed_for_user=False,
        open_at=announced,
        is_opened=True,
        uses_open_schedule=True,
        is_paused=False,
        custom_form=[],
    )
    published = [
        NaverSlot.from_payload(slot_payload(unitStartTime="2026-08-17 14:10:00")),
        NaverSlot.from_payload(slot_payload(unitStartTime="2026-08-26 14:10:00")),
    ]
    monkeypatch.setattr(api, "fetch_slots", lambda *_args, **_kwargs: published)

    assert api.resolve_target_open_at("2026-08-17", meta) == announced


def test_unopened_one_time_item_keeps_its_announced_open_time(monkeypatch):
    api = NaverBookingApi("1498729", "7531350", "12")
    announced = datetime(2026, 11, 27, 0, 0, tzinfo=KST)
    meta = NaverItemMeta(
        name="채널27",
        server_time=datetime(2026, 8, 2, 17, 51, tzinfo=KST),
        is_closed_booking=False,
        is_closed_for_user=False,
        open_at=announced,
        is_opened=False,
        uses_open_schedule=True,
        is_paused=False,
        custom_form=[],
    )
    monkeypatch.setattr(
        api,
        "fetch_slots",
        lambda *_args, **_kwargs: [],
    )

    assert api.resolve_target_open_at("2026-12-01", meta) == announced


def test_unopened_flag_with_published_history_is_still_treated_as_rolling(monkeypatch):
    """isOpened can be false early while an established rolling calendar exists."""
    api = NaverBookingApi("1325520", "6446475", "12")
    meta = NaverItemMeta(
        name="버디",
        server_time=datetime(2026, 8, 2, 9, 0, tzinfo=KST),
        is_closed_booking=False,
        is_closed_for_user=False,
        open_at=datetime(2026, 8, 1, 22, 0, tzinfo=KST),
        is_opened=False,
        uses_open_schedule=True,
        is_paused=False,
        custom_form=[],
    )
    published = [
        NaverSlot.from_payload(slot_payload(unitStartTime="2026-08-08 22:40:00")),
    ]
    monkeypatch.setattr(api, "fetch_slots", lambda *_args, **_kwargs: published)

    assert api.resolve_target_open_at("2026-08-09", meta) == datetime(
        2026, 8, 2, 22, 0, tzinfo=KST
    )


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


def test_precise_clock_sync_prefers_consistent_low_rtt_samples(monkeypatch):
    import engines.naver_api as module

    server_times = [
        datetime(2026, 8, 30, 23, 59, 30, 100000, tzinfo=KST),
        datetime(2026, 8, 30, 23, 59, 31, 200000, tzinfo=KST),
        datetime(2026, 8, 30, 23, 59, 32, 300000, tzinfo=KST),
    ]

    class Api:
        calls = 0

        def fetch_item_meta(self):
            value = server_times[self.calls]
            self.calls += 1
            return type("Meta", (), {"server_time": value})()

    moments = iter((0.0, 0.20, 1.0, 1.04, 2.0, 2.10, 2.10))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(moments))
    api = Api()
    clock = NaverServerClock(api)

    assert clock.sync_precise(3) is True
    assert api.calls == 3
    # The two agreeing samples win over the isolated first reply. The selected
    # sample's uncertainty also grows slightly while later reads complete.
    assert clock.last_precision == pytest.approx(
        0.02 + 1.06 * clock.DRIFT_SECONDS_PER_SECOND
    )
    assert clock._last_diagnostic["raw_spread_ms"] == pytest.approx(300.0)
    # currentDateTime is emitted near response completion, so anchoring it to
    # the request midpoint would make the clock run half an RTT ahead.
    assert clock._anchor_monotonic == pytest.approx(1.04)
    assert clock._anchor_server == pytest.approx(server_times[1].timestamp())


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


def test_submit_booking_duplicate_is_not_misclassified_as_sold_out(monkeypatch):
    from engines.naver_api import SubmitOutcome

    body = {
        "errors": [
            {
                "message": "Duplicated",
                "extensions": {"code": "BAD_USER_INPUT"},
            }
        ]
    }
    api, _ = api_with(monkeypatch, body)

    assert api.submit_booking({"businessId": "1498729"}).outcome == SubmitOutcome.DUPLICATED


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
    assert (
        classify_submit_error(
            "BAD_USER_INPUT",
            "예약 요청을 처리하지 못했습니다.",
            "BOOKING_NOT_AVAILABLE",
        )
        == SubmitOutcome.REFUSED
    )
    assert (
        classify_submit_error("BAD_USER_INPUT", "Duplicated")
        == SubmitOutcome.DUPLICATED
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


def test_replace_cookies_removes_previous_naver_account():
    api = NaverBookingApi("1498729", "7094790", "12")
    api.attach_cookies([
        {"name": "NID_AUT", "value": "old", "domain": ".naver.com", "path": "/"},
    ])
    api.session.cookies.set("KEEP", "other", domain="example.com", path="/")

    added = api.replace_cookies([
        {"name": "NID_SES", "value": "new", "domain": ".naver.com", "path": "/"},
    ])

    names = {(cookie.domain, cookie.name, cookie.value) for cookie in api.session.cookies}
    assert added == 1
    assert (".naver.com", "NID_AUT", "old") not in names
    assert (".naver.com", "NID_SES", "new") in names
    assert ("example.com", "KEEP", "other") in names
