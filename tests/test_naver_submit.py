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


def test_builder_rejects_unsupported_seat_period_and_npay_products():
    for field in ("isSeatUsed", "isPeriodFixed", "isNPayUsed"):
        item = deepcopy(BIZ_ITEM)
        item[field] = True

        result = prepare(biz_item=item)

        assert result.ready is False
        assert "지원하지" in result.reason


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

    assert result.outcome == SubmitOutcome.ERROR
    assert "csrf-secret" not in result.detail
    assert "01012345678" not in result.detail
    assert "RuntimeError" in result.detail


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
