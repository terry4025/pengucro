"""Direct-submit payload and browser transport contracts for Naver Booking."""

import asyncio
import pytest
from copy import deepcopy

from engines.naver_api import NaverAccount, SubmitOutcome


def account_booking_row(booking_id="new", status="RC03", name="바야흐로, 여름이었다."):
    return {
        "id": booking_id, "businessId": "1498729", "bizItemName": name,
        "formattedBookingDateText": "2026. 9. 13. 오후 12:50",
        "bookingStatusCode": status,
        "landingUrl": "https://m.booking.naver.com/my/bookings/999888",
    }


def account_booking_response(rows=(), *, has_more=False):
    return {"status": 200, "body": {"data": {"me": {
        "__typename": "MeSucceed", "upcomingBookings": {
            "bookings": list(rows), "pageInfo": {"hasNextPage": has_more},
        },
    }}}}


class AccountReadPage:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.created = 0
        self.closed = 0
        self.context = self

    async def new_page(self):
        self.created += 1
        return self

    async def goto(self, *args, **kwargs):
        pass

    async def evaluate(self, _script, request):
        self.calls.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self):
        self.closed += 1


ACCOUNT_TARGET = dict(target_date="2026-09-13", target_time="12:50", business_id="1498729", item_name="바야흐로,여름이었다.")


@pytest.mark.parametrize("response,expected", [
    (account_booking_response(), "not_found"),
    ({"status": 401, "body": {}}, "auth_error"),
    ({"status": 200, "body": {"errors": [{"message": "Cannot query field private-value"}]}}, "schema_error"),
    ({"status": 200, "body": {"data": {"me": None}}}, "auth_error"),
    ({"status": 200, "body": {"data": {"me": {}}}}, "schema_error"),
    (account_booking_response([{}]), "schema_error"),
    ({"status": 429, "body": {}}, "http_error"),
    (TimeoutError("private-value"), "network_error"),
])
def test_account_reconciliation_distinguishes_empty_auth_schema_and_transport(response, expected):
    from engines.naver_submit import NaverBrowserSubmitter
    page = AccountReadPage([response])
    evidence = asyncio.run(NaverBrowserSubmitter(page).reconcile_upcoming_booking(**ACCOUNT_TARGET, attempts=1))
    assert evidence.state == expected
    assert evidence.found is False
    assert evidence.successful_reads == (1 if expected == "not_found" else 0)
    assert evidence.failed_reads == (0 if expected == "not_found" else 1)
    assert "private-value" not in repr(evidence)
    assert page.closed == 1


def test_preflight_baseline_reuses_page_and_never_claims_old_booking_is_new():
    from engines.naver_submit import NaverBrowserSubmitter
    response = account_booking_response([account_booking_row("old")])
    page = AccountReadPage([response, response])
    submitter = NaverBrowserSubmitter(page)
    async def run():
        baseline = await submitter.preflight_reconciliation()
        assert baseline.complete and baseline.booking_ids == {"old"}
        return await submitter.reconcile_upcoming_booking(**ACCOUNT_TARGET, attempts=1)
    evidence = asyncio.run(run())
    assert evidence.state == "existing" and evidence.booking_id == "old"
    assert not evidence.found and evidence.baseline_checked
    assert page.created == 1 and page.closed == 1


def test_preflight_paginates_and_reconciles_new_booking():
    from engines.naver_submit import NaverBrowserSubmitter
    page = AccountReadPage([
        account_booking_response([account_booking_row("old1")], has_more=True),
        account_booking_response([account_booking_row("old2")]),
        account_booking_response([account_booking_row("new")]),
    ])
    submitter = NaverBrowserSubmitter(page)
    async def run():
        baseline = await submitter.preflight_reconciliation()
        assert baseline.complete and baseline.booking_ids == {"old1", "old2"}
        return await submitter.reconcile_upcoming_booking(**ACCOUNT_TARGET, attempts=1)
    evidence = asyncio.run(run())
    assert evidence.found and evidence.baseline_checked and evidence.status == "RC03"
    assert [request["page"] for request in page.calls] == [1, 2, 1]


def test_partial_baseline_still_rejects_known_existing_booking():
    from engines.naver_submit import NaverBrowserSubmitter
    response = account_booking_response([account_booking_row("old")], has_more=True)
    page = AccountReadPage([response, response])
    submitter = NaverBrowserSubmitter(page)
    async def run():
        assert not (await submitter.preflight_reconciliation(max_pages=1)).complete
        return await submitter.reconcile_upcoming_booking(**ACCOUNT_TARGET, attempts=1)
    evidence = asyncio.run(run())
    assert not evidence.found and evidence.state == "existing"
    assert not evidence.baseline_checked


def test_delayed_booking_after_failed_read_reports_both_read_counts(monkeypatch):
    from engines.naver_submit import NaverBrowserSubmitter
    async def no_delay(_seconds):
        pass
    monkeypatch.setattr("engines.naver_submit.asyncio.sleep", no_delay)
    page = AccountReadPage([TimeoutError(), account_booking_response(), account_booking_response([account_booking_row()])])
    evidence = asyncio.run(NaverBrowserSubmitter(page).reconcile_upcoming_booking(**ACCOUNT_TARGET, attempts=3))
    assert evidence.found and evidence.attempts == 3
    assert evidence.successful_reads == 2 and evidence.failed_reads == 1
    assert evidence.error_kind == "timeout"


def test_reconciliation_follows_next_pages_until_target_is_observed(monkeypatch):
    from engines.naver_submit import NaverBrowserSubmitter
    async def no_delay(_seconds):
        pass
    monkeypatch.setattr("engines.naver_submit.asyncio.sleep", no_delay)
    page = AccountReadPage([account_booking_response(has_more=True), account_booking_response(has_more=True), account_booking_response([account_booking_row()])])
    evidence = asyncio.run(NaverBrowserSubmitter(page).reconcile_upcoming_booking(**ACCOUNT_TARGET, attempts=3))
    assert evidence.found
    assert [request["page"] for request in page.calls] == [1, 2, 3]


def test_cancelled_preflight_closes_lookup_page_and_leaves_no_baseline():
    from engines.naver_submit import NaverBrowserSubmitter
    class HangingAccountPage(AccountReadPage):
        async def evaluate(self, *_args):
            await asyncio.Event().wait()
    page = HangingAccountPage([])
    submitter = NaverBrowserSubmitter(page)
    async def run():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(submitter.preflight_reconciliation(), timeout=0.01)
    asyncio.run(run())
    assert page.closed == 1 and submitter.reconciliation_baseline is None


def test_stop_event_prevents_reconciliation_network_reads():
    from engines.naver_submit import NaverBrowserSubmitter
    from threading import Event
    stopped = Event()
    stopped.set()
    page = AccountReadPage([])
    evidence = asyncio.run(NaverBrowserSubmitter(page).reconcile_upcoming_booking(**ACCOUNT_TARGET, stop_event=stopped))
    assert evidence.state == "stopped" and page.calls == []


def test_account_change_can_invalidate_baseline_synchronously():
    from engines.naver_submit import NaverBrowserSubmitter, NaverBookingSnapshot
    submitter = NaverBrowserSubmitter(AccountReadPage([]))
    submitter.reconciliation_baseline = NaverBookingSnapshot("ok", booking_ids=frozenset({"old"}), complete=True)
    submitter.reset_reconciliation_baseline()
    assert submitter.reconciliation_baseline is None


@pytest.mark.parametrize("response", [account_booking_response(), {"status": 401, "body": {}}])
def test_preflight_restores_main_page_after_read_success_or_error(response):
    from engines.naver_submit import NaverBrowserSubmitter
    lookup = AccountReadPage([response])
    class MainPage:
        context = lookup
        foreground_calls = 0
        def is_closed(self):
            return False
        async def bring_to_front(self):
            self.foreground_calls += 1
    main = MainPage()
    submitter = NaverBrowserSubmitter(main)
    async def run():
        await submitter.preflight_reconciliation()
        await submitter.close_reconciliation_page()
    asyncio.run(run())
    assert main.foreground_calls == 1
    assert submitter.last_foreground_restore == "restored"


def test_preflight_does_not_focus_closed_main_page():
    from engines.naver_submit import NaverBrowserSubmitter
    class ClosedMain:
        context = AccountReadPage([account_booking_response()])
        def is_closed(self):
            return True
        async def bring_to_front(self):
            raise AssertionError("closed page must not be focused")
    submitter = NaverBrowserSubmitter(ClosedMain())
    async def run():
        await submitter.preflight_reconciliation()
        await submitter.close_reconciliation_page()
    asyncio.run(run())
    assert submitter.last_foreground_restore == "closed"


@pytest.mark.parametrize("status", ["CANCELLED", "RC04", "", "UNKNOWN"])
def test_matching_cancelled_or_unverified_status_is_not_active_booking(status):
    from engines.naver_submit import match_upcoming_booking
    evidence = match_upcoming_booking([account_booking_row(status=status)], **ACCOUNT_TARGET)
    assert not evidence.found and evidence.state == "invalid_status"
    baseline_match = match_upcoming_booking([account_booking_row(status=status)], **ACCOUNT_TARGET, baseline_booking_ids={"new"})
    assert not baseline_match.found and baseline_match.state == "invalid_status"


def test_item_substrings_and_multiple_matching_reservations_are_not_success():
    from engines.naver_submit import match_upcoming_booking
    wrong = match_upcoming_booking([account_booking_row(name="여름")], **ACCOUNT_TARGET)
    multiple = match_upcoming_booking([account_booking_row("one"), account_booking_row("two")], **ACCOUNT_TARGET)
    assert not wrong.found
    assert not multiple.found and multiple.state == "ambiguous_match"


@pytest.mark.parametrize("status", ["RC02", "RC03"])
def test_active_booking_status_is_preserved_for_pending_vs_confirmed_display(status):
    from engines.naver_submit import match_upcoming_booking
    evidence = match_upcoming_booking([account_booking_row(status=status)], **ACCOUNT_TARGET)
    assert evidence.found and evidence.status == status


@pytest.mark.parametrize("url,retained", [
    ("https://order.pay.naver.com/orderSheet/test", True),
    ("https://order.pay.naver.com.evil.test/my/bookings/999888", False),
    ("http://order.pay.naver.com/orderSheet/test", False),
    ("https://evil.test/?bookingId=999888", False),
    ("https://evil.test/my/bookings/999888", False),
    ("https://user:secret@order.pay.naver.com/orderSheet/test", False),
])
def test_partial_checkout_url_is_evidence_only_and_untrusted_urls_never_succeed(url, retained):
    from engines.naver_submit import _submit_result_from_response
    evidence = _submit_result_from_response({"status": 200, "body": {
        "data": {"submitBooking": {"bookingId": None, "url": {"pc": url}}},
        "errors": [{"message": "RT47", "extensions": {"code": "RT47", "reason": "OUT_OF_STOCK"}}],
    }})
    assert evidence.outcome == (SubmitOutcome.UNKNOWN if retained else SubmitOutcome.REFUSED)
    assert not evidence.booking_id
    assert evidence.url == (url if retained else None)


def test_partial_checkout_url_never_permits_not_open_retry():
    from engines.naver_submit import _submit_result_from_response
    evidence = _submit_result_from_response({"status": 200, "body": {
        "data": {"submitBooking": {"url": "https://order.pay.naver.com/orderSheet/test"}},
        "errors": [{"message": "BizItem is not opened."}],
    }})
    assert evidence.outcome == SubmitOutcome.UNKNOWN


def test_safe_browser_rtt_samples_expire_and_failed_reads_do_not_refresh(monkeypatch):
    from engines.naver_submit import NaverBrowserSubmitter
    clock = [100.0]
    monkeypatch.setattr("engines.naver_submit.time.monotonic", lambda: clock[0])
    class TimedPage(FakePage):
        async def evaluate(self, *args):
            response = await super().evaluate(*args)
            response["elapsedMs"] = 10.0
            return response
    submitter = NaverBrowserSubmitter(TimedPage({"data": {"account": {"isLoggedIn": True}}}))
    asyncio.run(submitter.fetch_account())
    assert submitter.recent_safe_rtt_samples() == [0.01]
    assert submitter.last_safe_rtt_at == 100.0
    clock[0] = 116.0
    assert submitter.recent_safe_rtt_samples() == []
    submitter.page.body = {"errors": [{"message": "network failure"}]}
    asyncio.run(submitter.fetch_account())
    assert submitter.last_safe_rtt_at == 100.0


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
        if argument is None:
            return {"now": 1000, "origin": 1000000}
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
        if argument is None:
            return {"now": 1000, "origin": 1000000}
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
                "serverOpenAt": 1060,
                "clockRttMs": 60,
                "clockSampleCount": 5,
                "clockUncertaintyMs": 30,
                "clockSpreadMs": 4,
                "estimatedOutboundMs": 30,
                "targetArrivalBeforeOpenMs": 60,
                "appliedLeadMs": 90,
                "startedAt": 1000,
                "lastStartedAt": 1010,
                "headersAt": 1020,
                "responseBodyAt": 1040,
                "completedAt": 1042,
                "attempts": 2,
                "notOpenAttempts": 1,
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


def test_browser_submitter_keeps_partial_booking_url_as_unverified_candidate():
    from engines.naver_submit import NaverBrowserSubmitter

    page = FakePage({
        "data": {"submitBooking": {
            "bookingId": None,
            "url": "/my/bookings/999888",
        }},
        "errors": [{
            "message": "RT47",
            "extensions": {"code": "RT47", "reason": "정원 마감"},
        }],
    })

    result = asyncio.run(
        NaverBrowserSubmitter(page).submit({"slotId": "1331382668"})
    )

    assert result.outcome == SubmitOutcome.UNKNOWN
    assert not result.booking_id
    assert result.url == "https://m.booking.naver.com/my/bookings/999888"


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
    assert submitter.last_armed_timing["estimatedOutboundMs"] == 30
    assert submitter.last_armed_diagnostics == {
        "attempts": 2.0,
        "notOpenAttempts": 1.0,
        "httpStatus": 200.0,
        "ttfbMs": 10.0,
        "responseMs": 30.0,
        "attemptTimings": [],
        "armedVisibility": "unknown",
        "dispatchVisibility": "unknown",
        "foregroundRestore": "unavailable",
    }
    assert page.calls[0]["delayMs"] == 125
    assert page.calls[0]["input"]["slotId"] == "1331382668"
    assert page.calls[0]["maxAttempts"] == 3
    assert page.calls[0]["retryDelayMs"] == 10
    assert page.calls[0]["retryWindowMs"] == 500
    assert page.calls[0]["notOpenCodes"] == ["BizItem is not opened."]


def test_browser_submitter_transfers_synced_server_deadline():
    from engines.naver_submit import NaverBrowserSubmitter

    page = ArmedPage()
    submitter = NaverBrowserSubmitter(page)
    payload = {
        "businessId": "1498729",
        "bizItemId": "7094790",
        "slotId": "1331382668",
        "csrfToken": "csrf-secret",
    }

    asyncio.run(submitter.arm_submit_at_server_time(
        payload,
        4.75,
        open_at_epoch=1788102000.0,
        lead_seconds=0.115,
        retry_lead_seconds=0.055,
        target_arrival_before_open_seconds=0.060,
    ))

    request = page.calls[0]
    assert request["openAtEpochMs"] == 1788102000000
    assert request["leadMs"] == 115
    assert request["retryLeadMs"] == 55
    assert "clockSamples" not in request
    assert request["targetArrivalBeforeOpenMs"] == 60
    assert request["serverOpenAtPerfMs"] - request["dueAtPerfMs"] == pytest.approx(115)
    assert request["clockOrigin"] == 1000000


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
