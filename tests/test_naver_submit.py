"""Direct-submit payload and browser transport contracts for Naver Booking."""

import asyncio
from copy import deepcopy

from engines.naver_api import NaverAccount, SubmitOutcome


BUSINESS = {
    "businessId": "1498729",
    "businessTypeId": 12,
    "rawNames": {
        "name": "제로월드",
        "serviceName": "방탈출 예약",
    },
    "addressJson": {"road": "서울"},
    "bookingTimeUnitCode": "RT03",
    "translationStatusJson": {},
    "agencies": [],
    "bookingConfirmCode": "CF02",
    "nPayRegStatusCode": None,
    "uncompletedBookingProcessCode": None,
    "uncompletedBookingRefundRate": None,
    "refundPolicy": {"refundPolicyId": 17},
    "businessResources": [{"resourceUrl": "https://example.test/business.jpg"}],
    "customFormJson": [{
        "type": "SELECT",
        "title": "예약인원 선택",
        "originalTitle": "예약인원 선택",
        "required": "y",
        "options": [
            {"idx": 0, "value": "2인", "originalValue": "2인"},
            {"idx": 1, "value": "3인", "originalValue": "3인"},
            {"idx": 2, "value": "4인", "originalValue": "4인"},
        ],
        "perItem": "n",
        "isTemporal": "n",
        "isPersonalInfo": "n",
    }],
}

BIZ_ITEM = {
    "bizItemId": "7094790",
    "name": "사요나라, 세이코!",
    "isNPayUsed": False,
    "isPeriodFixed": False,
    "isSeatUsed": False,
    "addressJson": {},
    "bookingConfirmCode": "CF01",
    "paymentSettingJson": {},
    "resources": [{"resourceUrl": "https://example.test/item.jpg"}],
}

SLOT = {
    "id": "7094790_1314916760_1331382668_2026-08-08T14:30:00+09:00",
    "name": "",
    "slotId": "1331382668",
    "scheduleId": "1314916760",
    "detailScheduleId": None,
    "unitStartDateTime": "2026-08-08T05:30:00Z",
    "unitStartTime": "2026-08-08 14:30:00",
    "unitBookingCount": 0,
    "unitStock": 1,
    "bookingCount": 0,
    "occupiedBookingCount": 0,
    "stock": 1,
    "isBusinessDay": True,
    "isSaleDay": True,
    "isUnitSaleDay": True,
    "isUnitBusinessDay": True,
    "isHoliday": False,
    "duration": None,
    "desc": "",
    "minBookingCount": 1,
    "maxBookingCount": 1,
    "saleStartDateTime": None,
    "saleEndDateTime": None,
    "seatGroups": [],
    "prices": [{
        "groupName": None,
        "isDefault": False,
        "price": 33000,
        "priceId": "8895079",
        "scheduleId": None,
        "priceTypeCode": None,
        "name": "1인",
        "normalPrice": 33000,
        "desc": "1인 이용금액",
        "order": 0,
        "groupOrder": 0,
        "slotId": "1331382668",
        "agencyKey": None,
        "bookingCount": None,
        "isImp": True,
        "saleStartDateTime": None,
        "saleEndDateTime": None,
    }],
}

RESERVATION = {
    "name": "홍길동",
    "phone": "010-1234-5678",
    "email": "",
    "people": "3",
    "reservationDate": "2026-08-08",
    "reservationTime": "14:30",
}

ACCOUNT = NaverAccount(
    is_logged_in=True,
    csrf_token="csrf-secret",
    is_sms_alarm=False,
    user_id="user-1",
    nickname="tester",
)


def prepare(**overrides):
    from engines.naver_submit import NaverSubmitPayloadBuilder

    values = {
        "business": deepcopy(BUSINESS),
        "biz_item": deepcopy(BIZ_ITEM),
        "slot": deepcopy(SLOT),
        "account": ACCOUNT,
        "reservation": deepcopy(RESERVATION),
    }
    values.update(overrides)
    return NaverSubmitPayloadBuilder().prepare(**values)


def test_builder_produces_supported_episode_submit_input():
    result = prepare()

    assert result.ready is True
    assert result.slot_id == "1331382668"
    assert result.payload["businessId"] == "1498729"
    assert result.payload["bizItemId"] == "7094790"
    assert result.payload["slotId"] == "1331382668"
    assert result.payload["startDateTime"] == "2026-08-08T05:30:00.000Z"
    assert result.payload["endDateTime"] == "2026-08-08T05:30:00.000Z"
    assert result.payload["startMinute"] == 870
    assert result.payload["endMinute"] == 870
    assert result.payload["price"] == 33000
    assert result.payload["bizItemPrice"] == 33000
    assert result.payload["priceTypeJson"][0]["bookingCount"] == 1
    assert result.payload["phone"] == "01012345678"
    assert result.payload["customFormInputJson"][0]["value"] == "3인"
    assert result.payload["customFormInputJson"][0]["originalValue"] == "3인"
    assert result.payload["termsVersion"] == "20251030"
    assert result.payload["csrfToken"] == "csrf-secret"
    assert result.payload["bookingConfirmCode"] == "CF01"
    assert result.payload["todayDealRate"] is None


def test_builder_rejects_account_without_login_or_csrf():
    account = NaverAccount(False, "", False)

    result = prepare(account=account)

    assert result.ready is False
    assert "로그인" in result.reason
    assert "csrf-secret" not in result.reason


def test_builder_rejects_unsupported_seat_and_period_products():
    for field in ("isSeatUsed", "isPeriodFixed"):
        item = deepcopy(BIZ_ITEM)
        item[field] = True

        result = prepare(biz_item=item)

        assert result.ready is False
        assert "지원하지" in result.reason


def test_builder_supports_npay_prepaid_without_page_refresh():
    item = deepcopy(BIZ_ITEM)
    item["isNPayUsed"] = True
    item["paymentSettingJson"] = None
    slot = deepcopy(SLOT)
    slot["_isPostPaymentResolved"] = True
    slot["isPostPayment"] = False

    result = prepare(biz_item=item, slot=slot)

    assert result.ready is True
    assert result.requires_checkout is True
    assert result.payment_label == "네이버페이 선결제형"
    assert result.payment_source == "slotSeat.isPostPayment"
    assert result.payload["isNPayUsed"] is True
    assert result.payload["isPostPayment"] is False
    assert result.payload["paymentSettingJson"] is None


def test_builder_supports_npay_postpaid_as_booking_completion():
    item = deepcopy(BIZ_ITEM)
    item["isNPayUsed"] = True
    item["paymentSettingJson"] = None
    slot = deepcopy(SLOT)
    slot["_isPostPaymentResolved"] = True
    slot["isPostPayment"] = True

    result = prepare(biz_item=item, slot=slot)

    assert result.ready is True
    assert result.requires_checkout is False
    assert result.payment_label == "후결제형"
    assert result.payload["isNPayUsed"] is True
    assert result.payload["isPostPayment"] is True


def test_builder_uses_item_payment_moment_before_slot_detail_is_available():
    item = deepcopy(BIZ_ITEM)
    item["isNPayUsed"] = True
    item["paymentSettingJson"] = {
        "paymentMoment": "POST",
        "userSelectedPaymentMethod": "ONSITE",
    }

    result = prepare(biz_item=item)

    assert result.ready is True
    assert result.payment_label == "후결제형"
    assert result.payment_source == "bizItem.paymentSettingJson.paymentMoment"
    assert result.payload["paymentSettingJson"]["userSelectedPaymentMethod"] == "ONSITE"


def test_builder_rejects_slot_that_does_not_match_target_time():
    slot = deepcopy(SLOT)
    slot["unitStartDateTime"] = "2026-08-08T06:30:00Z"

    result = prepare(slot=slot)

    assert result.ready is False
    assert "예약 시각" in result.reason


def test_builder_rejects_unanswered_required_custom_form():
    reservation = deepcopy(RESERVATION)
    reservation["people"] = "9"

    result = prepare(reservation=reservation)

    assert result.ready is False
    assert "필수 추가 입력" in result.reason
    assert "홍길동" not in result.reason
    assert "01012345678" not in result.reason


def test_builder_supports_ticketing_business_and_all_dynamic_questions():
    business = deepcopy(BUSINESS)
    business["businessTypeId"] = 99
    business["customFormJson"] = [
        {
            "type": "SELECT",
            "title": "예약 매수",
            "required": "y",
            "options": [{"value": "2매"}, {"value": "3매"}],
        },
        {
            "type": "SELECT",
            "title": "주의사항",
            "required": "y",
            "options": [{"value": "확인했습니다"}, {"value": "미동의"}],
        },
        {"type": "TEXTAREA", "title": "필수 답변", "required": "y"},
    ]
    slot = deepcopy(SLOT)
    slot["maxBookingCount"] = 10
    slot["unitStock"] = 10

    result = prepare(business=business, slot=slot)

    assert result.ready is True
    assert result.payload["businessTypeId"] == 99
    assert result.payload["bookingCount"] == 3
    assert result.payload["price"] == 99000
    assert [question["value"] for question in result.payload["customFormInputJson"]] == [
        "3매", "확인했습니다", "확인했습니다"
    ]


def test_builder_uses_item_level_form_before_business_form():
    item = deepcopy(BIZ_ITEM)
    item["customFormJson"] = [{
        "type": "TEXT", "title": "관람자 이름", "required": "y"
    }]

    result = prepare(biz_item=item)

    assert result.ready is True
    assert len(result.payload["customFormInputJson"]) == 1
    assert result.payload["customFormInputJson"][0]["value"] == "홍길동"


def test_builder_uses_people_as_ticket_count_for_type_12_quantity_slot():
    business = deepcopy(BUSINESS)
    business["customFormJson"] = [{
        "type": "SELECT",
        "title": "1매예약은 1명예약 입니다. 즉 2명일경우 최소2매 예약",
        "required": "y",
        "options": [{"value": "네 확인했습니다"}],
    }]
    slot = deepcopy(SLOT)
    slot["minBookingCount"] = None
    slot["maxBookingCount"] = None
    slot["unitStock"] = 8
    slot["unitBookingCount"] = 0

    result = prepare(business=business, slot=slot)

    assert result.ready is True
    assert result.quantity_mode is True
    assert result.available_count == 8
    assert result.payload["businessTypeId"] == 12
    assert result.payload["bookingCount"] == 3
    assert result.payload["priceTypeJson"][0]["bookingCount"] == 3
    assert result.payload["price"] == 99000
    assert result.payload["customFormInputJson"][0]["value"] == "네 확인했습니다"


def test_builder_keeps_people_separate_from_fixed_single_slot_booking():
    result = prepare()

    assert result.ready is True
    assert result.quantity_mode is False
    assert result.payload["bookingCount"] == 1
    assert result.payload["priceTypeJson"][0]["bookingCount"] == 1


def test_builder_rejects_ticket_count_above_remaining_stock():
    slot = deepcopy(SLOT)
    slot["minBookingCount"] = None
    slot["maxBookingCount"] = None
    slot["unitStock"] = 8
    slot["unitBookingCount"] = 7

    result = prepare(slot=slot)

    assert result.ready is False
    assert result.quantity_mode is True
    assert result.available_count == 1
    assert "잔여 수량" in result.reason


class FakePage:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.calls = []

    async def evaluate(self, _script, argument=None):
        self.calls.append(argument)
        return {"status": self.status, "body": self.body}


class RaisingPage:
    async def evaluate(self, _script, _argument=None):
        raise RuntimeError("csrf-secret 01012345678")


class HangingPage:
    async def evaluate(self, _script, _argument=None):
        await asyncio.Event().wait()


class DelayedPage(FakePage):
    async def evaluate(self, script, argument=None):
        await asyncio.sleep(0.02)
        return await super().evaluate(script, argument)


class BrowserTimedPage(DelayedPage):
    async def evaluate(self, script, argument=None):
        response = await super().evaluate(script, argument)
        response["elapsedMs"] = 8.0
        return response


class ArmedPage:
    def __init__(self):
        self.calls = []
        self.arm_id = ""

    async def evaluate(self, _script, argument=None):
        self.calls.append(argument)
        if "delayMs" in argument:
            self.arm_id = argument["armId"]
            return {"id": self.arm_id, "status": "armed", "delayMs": argument["delayMs"]}
        if set(argument) == {"armId"}:
            return {
                "status": "complete",
                "response": {"status": 200, "body": {"data": {"submitBooking": {
                    "bookingId": "999888", "url": "/my/bookings/999888",
                }}}},
                "armedAt": 800,
                "dueAt": 995,
                "startedAt": 1000,
                "lastStartedAt": 1010,
                "completedAt": 1042,
                "attempts": 2,
                "error": "",
            }
        return True


def test_browser_submitter_classifies_success():
    from engines.naver_submit import NaverBrowserSubmitter

    page = FakePage({"data": {"submitBooking": {
        "bookingId": "999888",
        "url": "/my/bookings/999888",
    }}})

    result = asyncio.run(
        NaverBrowserSubmitter(page).submit({"slotId": "1331382668"})
    )

    assert result.outcome == SubmitOutcome.SUCCESS
    assert result.booking_id == "999888"
    assert page.calls[0]["operationName"] == "submitBooking"
    assert page.calls[0]["variables"]["input"]["slotId"] == "1331382668"


def test_browser_submitter_prefers_partial_booking_id_over_rt47_error():
    from engines.naver_submit import NaverBrowserSubmitter

    page = FakePage({
        "data": {"submitBooking": {
            "bookingId": "999888",
            "url": {"pc": "https://order.pay.naver.com/orderSheet/test"},
        }},
        "errors": [{
            "message": "RT47",
            "extensions": {"code": "RT47", "reason": "정원 마감"},
        }],
    })

    result = asyncio.run(
        NaverBrowserSubmitter(page).submit({"slotId": "1331382668"})
    )

    assert result.outcome == SubmitOutcome.SUCCESS
    assert result.booking_id == "999888"


def test_upcoming_booking_match_requires_business_item_date_and_time():
    from engines.naver_submit import match_upcoming_booking

    rows = [{
        "id": "999888",
        "businessId": "1498729",
        "bizItemName": "바야흐로, 여름이었다.",
        "formattedBookingDateText": "2026. 9. 5.(토) 오후 2:25",
        "bookingStatusCode": "RC02",
        "landingUrl": "https://m.booking.naver.com/my/bookings/999888",
    }]

    matched = match_upcoming_booking(
        rows,
        target_date="2026-09-05",
        target_time="14:25",
        business_id="1498729",
        item_name="바야흐로,여름이었다.",
    )
    wrong_time = match_upcoming_booking(
        rows,
        target_date="2026-09-05",
        target_time="15:25",
        business_id="1498729",
        item_name="바야흐로,여름이었다.",
    )

    assert matched.found is True
    assert matched.booking_id == "999888"
    assert wrong_time.found is False


def test_browser_reconciliation_reads_account_history_without_mutation():
    from engines.naver_submit import NaverBrowserSubmitter

    class LookupPage:
        def __init__(self):
            self.goto_calls = []
            self.evaluate_calls = []
            self.closed = False

        async def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))

        async def evaluate(self, _script, argument):
            self.evaluate_calls.append(argument)
            return {"status": 200, "body": {"data": {"me": {
                "upcomingBookings": {"bookings": [{
                    "id": "999888",
                    "businessId": "1498729",
                    "bizItemName": "바야흐로, 여름이었다.",
                    "formattedBookingDateText": "2026. 9. 5.(토) 오후 2:25",
                    "bookingStatusCode": "RC02",
                    "landingUrl": "https://m.booking.naver.com/my/bookings/999888",
                }]},
            }}}}

        async def close(self):
            self.closed = True

    lookup = LookupPage()

    class Context:
        async def new_page(self):
            return lookup

    source = type("SourcePage", (), {"context": Context()})()
    result = asyncio.run(NaverBrowserSubmitter(source).reconcile_upcoming_booking(
        target_date="2026-09-05",
        target_time="14:25",
        business_id="1498729",
        item_name="바야흐로,여름이었다.",
    ))

    assert result.found is True
    assert result.booking_id == "999888"
    assert lookup.evaluate_calls[0]["query"].startswith("query UpcomingBookingQuery")
    assert lookup.closed is True


def test_browser_submitter_classifies_not_open_and_rt98():
    from engines.naver_submit import NaverBrowserSubmitter

    not_open = FakePage({
        "errors": [{"message": "BizItem is not opened."}],
    })
    abuse = FakePage({
        "errors": [{
            "message": "RT98",
            "extensions": {"code": "RT98", "reason": "비정상 요청 탐지"},
        }],
    })

    not_open_result = asyncio.run(
        NaverBrowserSubmitter(not_open).submit({"slotId": "1331382668"})
    )
    abuse_result = asyncio.run(
        NaverBrowserSubmitter(abuse).submit({"slotId": "1331382668"})
    )

    assert not_open_result.outcome == SubmitOutcome.NOT_OPEN
    assert abuse_result.outcome == SubmitOutcome.ABUSE
    assert abuse_result.code == "RT98"


def test_browser_submitter_redacts_payload_from_transport_errors():
    from engines.naver_submit import NaverBrowserSubmitter

    result = asyncio.run(NaverBrowserSubmitter(RaisingPage()).submit({
        "csrfToken": "csrf-secret",
        "phone": "01012345678",
    }))

    assert result.outcome == SubmitOutcome.UNKNOWN
    assert "csrf-secret" not in result.detail
    assert "01012345678" not in result.detail
    assert "RuntimeError" in result.detail


def test_browser_submitter_bounds_an_ambiguous_submit_timeout():
    from engines.naver_submit import NaverBrowserSubmitter

    submitter = NaverBrowserSubmitter(HangingPage(), timeout_seconds=0.01)
    result = asyncio.run(submitter.submit({"slotId": "1331382668"}))

    assert result.outcome == SubmitOutcome.UNKNOWN
    assert "확인" in result.detail


def test_browser_submitter_measures_the_actual_browser_round_trip():
    from engines.naver_submit import NaverBrowserSubmitter

    page = DelayedPage({"data": {"account": {
        "isLoggedIn": True,
        "csrfToken": "csrf-secret",
        "isSmsAlarm": False,
    }}})
    submitter = NaverBrowserSubmitter(page)

    account = asyncio.run(submitter.fetch_account())

    assert account.is_logged_in is True
    assert submitter.last_rtt >= 0.015


def test_browser_submitter_prefers_in_page_rtt_over_cdp_elapsed_time():
    from engines.naver_submit import NaverBrowserSubmitter

    page = BrowserTimedPage({"data": {"account": {
        "isLoggedIn": True,
        "csrfToken": "csrf-secret",
        "isSmsAlarm": False,
    }}})
    submitter = NaverBrowserSubmitter(page)

    account = asyncio.run(submitter.fetch_account())

    assert account.is_logged_in is True
    assert submitter.last_rtt == 0.008
    assert submitter.safe_rtt_samples == [0.008]


def test_browser_submitter_arms_one_in_page_submit_and_reads_its_result():
    from engines.naver_submit import NaverBrowserSubmitter

    page = ArmedPage()
    submitter = NaverBrowserSubmitter(page)
    payload = {"slotId": "1331382668", "csrfToken": "csrf-secret"}

    arm_id = asyncio.run(submitter.arm_submit_at(payload, 0.125))
    state, result, elapsed_ms = asyncio.run(
        submitter.read_armed_submit(arm_id, payload)
    )

    assert state == "complete"
    assert result is not None and result.outcome == SubmitOutcome.SUCCESS
    assert result.booking_id == "999888"
    assert elapsed_ms == 42
    assert submitter.last_rtt == 0.042
    assert submitter.last_armed_timing["dueAt"] == 995
    assert submitter.last_armed_timing["startedAt"] == 1000
    assert submitter.last_armed_timing["lastStartedAt"] == 1010
    assert submitter.last_armed_timing["attempts"] == 2
    assert page.calls[0]["delayMs"] == 125
    assert page.calls[0]["input"]["slotId"] == "1331382668"
    assert page.calls[0]["maxAttempts"] == 3
    assert page.calls[0]["retryDelayMs"] == 10
    assert page.calls[0]["retryWindowMs"] == 350
    assert page.calls[0]["notOpenCodes"] == ["BizItem is not opened."]


def test_browser_submitter_classifies_the_resolver_reason():
    from engines.naver_submit import NaverBrowserSubmitter

    page = FakePage({
        "errors": [{
            "message": "예약 요청을 처리하지 못했습니다.",
            "extensions": {
                "code": "BAD_USER_INPUT",
                "reason": "BOOKING_NOT_AVAILABLE",
            },
        }],
    })

    result = asyncio.run(
        NaverBrowserSubmitter(page).submit({"slotId": "1331382668"})
    )

    assert result.outcome == SubmitOutcome.REFUSED


def test_browser_submitter_redacts_formatted_versions_of_phone_numbers():
    from engines.naver_submit import NaverBrowserSubmitter

    page = FakePage({
        "errors": [{
            "message": "BAD_USER_INPUT",
            "extensions": {
                "code": "BAD_USER_INPUT",
                "reason": "연락처 010-1234-5678 확인 필요",
            },
        }],
    })

    result = asyncio.run(NaverBrowserSubmitter(page).submit({
        "phone": "01012345678",
    }))

    assert "01012345678" not in result.detail
    assert "010-1234-5678" not in result.detail
    assert "[redacted]" in result.detail


def test_browser_submitter_redacts_secrets_echoed_by_server_errors():
    from engines.naver_submit import NaverBrowserSubmitter

    page = FakePage({
        "errors": [{
            "message": "BAD_USER_INPUT",
            "extensions": {
                "code": "BAD_USER_INPUT",
                "reason": "csrf-secret / 01012345678 / 홍길동",
            },
        }],
    })

    result = asyncio.run(NaverBrowserSubmitter(page).submit({
        "csrfToken": "csrf-secret",
        "phone": "01012345678",
        "name": "홍길동",
    }))

    assert result.outcome == SubmitOutcome.PAYLOAD
    assert "csrf-secret" not in result.detail
    assert "01012345678" not in result.detail
    assert "홍길동" not in result.detail
    assert "[redacted]" in result.detail


def test_browser_submitter_fetches_account_from_same_session():
    from engines.naver_submit import NaverBrowserSubmitter

    page = FakePage({"data": {"account": {
        "isLoggedIn": True,
        "csrfToken": "csrf-secret",
        "isSmsAlarm": True,
        "userId": "user-1",
        "nickname": "tester",
    }}})

    account = asyncio.run(NaverBrowserSubmitter(page).fetch_account())

    assert account.is_logged_in is True
    assert account.csrf_token == "csrf-secret"
    assert account.is_sms_alarm is True
    assert page.calls[0]["operationName"] == "account"
    assert NaverBrowserSubmitter(page).last_account_fetch_ok is False


def test_browser_submitter_marks_live_account_query_as_authoritative():
    from engines.naver_submit import NaverBrowserSubmitter

    submitter = NaverBrowserSubmitter(FakePage({"data": {"account": {
        "isLoggedIn": True,
        "csrfToken": "csrf-new-account",
        "isSmsAlarm": False,
        "userId": "new-user",
    }}}))

    account = asyncio.run(submitter.fetch_account())

    assert account.user_id == "new-user"
    assert submitter.last_account_fetch_ok is True


def test_browser_submitter_distinguishes_query_failure_from_logged_out():
    from engines.naver_submit import NaverBrowserSubmitter

    submitter = NaverBrowserSubmitter(RaisingPage())

    account = asyncio.run(submitter.fetch_account())

    assert account.is_logged_in is False
    assert submitter.last_account_fetch_ok is False
