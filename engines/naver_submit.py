"""Build and send Naver's direct ``submitBooking`` request.

The page itself still owns authentication. This module supports ordinary
single-slot reservations and multi-unit performance tickets, including Npay
pre-payment and post-payment. Seat and fixed-period products still fall back to
the browser because their payloads have materially different state.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from engines.naver_api import (
    ACCOUNT_QUERY,
    KST,
    SUBMIT_BOOKING_MUTATION,
    NaverAccount,
    SubmitOutcome,
    SubmitResult,
    classify_submit_error,
)
from engines.naver_forms import prepare_custom_form_answers


TERMS_VERSION = "20251030"
PAYMENT_BOOKING = "booking"
PAYMENT_NPAY_PREPAID = "npay_prepaid"
PAYMENT_POSTPAID = "postpaid"
UPCOMING_BOOKINGS_URL = "https://m.place.naver.com/my/timeline?tab=RESERVATION"
UPCOMING_BOOKINGS_ENDPOINT = "https://porta.place.naver.com/graphql"


UPCOMING_BOOKINGS_QUERY = """query UpcomingBookingQuery($page: Int, $limit: Int) {
  me {
    ... on MeSucceed {
      upcomingBookings(page: $page, limit: $limit) {
        bookings {
          id
          formattedBookingDateText
          bizItemName
          businessName
          businessId
          label
          bookingStatusCode
          landingUrl
          displayOrderTimestamp
        }
        pageInfo { page nextPage totalCount hasNextPage }
      }
    }
  }
}"""


@dataclass(frozen=True)
class NaverSubmitPreparation:
    ready: bool
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    slot_id: str = ""
    payment_mode: str = PAYMENT_BOOKING
    payment_source: str = ""
    quantity_mode: bool = False
    available_count: int | None = None

    @property
    def requires_checkout(self) -> bool:
        return self.payment_mode == PAYMENT_NPAY_PREPAID

    @property
    def payment_label(self) -> str:
        return {
            PAYMENT_BOOKING: "예약 완료형(후결제·현장결제)",
            PAYMENT_NPAY_PREPAID: "네이버페이 선결제형",
            PAYMENT_POSTPAID: "후결제형",
        }.get(self.payment_mode, self.payment_mode or "알 수 없음")


@dataclass(frozen=True)
class NaverBookingReconciliation:
    """Authoritative same-account evidence after an ambiguous mutation reply."""

    found: bool
    booking_id: str = ""
    url: str = ""
    status: str = ""


def _compact_match_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", str(value or "").casefold())


def _row_booking_datetime(
    row: Mapping[str, Any], target_date: str, target_time: str
) -> bool:
    """Match the date/time rendered by MY플레이스 without guessing from order."""
    try:
        target = datetime.fromisoformat(f"{target_date}T{target_time[:5]}").replace(
            tzinfo=KST
        )
    except ValueError:
        return False

    raw_timestamp = row.get("displayOrderTimestamp")
    parsed_timestamp: datetime | None = None
    if isinstance(raw_timestamp, (int, float)):
        epoch = float(raw_timestamp)
        if epoch > 10_000_000_000:
            epoch /= 1000
        try:
            parsed_timestamp = datetime.fromtimestamp(epoch, KST)
        except (OSError, OverflowError, ValueError):
            parsed_timestamp = None
    elif raw_timestamp:
        parsed_timestamp = _parse_datetime(raw_timestamp)
        if parsed_timestamp is not None:
            parsed_timestamp = parsed_timestamp.astimezone(KST)
    if parsed_timestamp is not None:
        timestamp_matches = (
            parsed_timestamp.date() == target.date()
            and parsed_timestamp.hour == target.hour
            and parsed_timestamp.minute == target.minute
        )
        if timestamp_matches:
            return True

    rendered = " ".join(
        str(row.get(key) or "")
        for key in ("formattedBookingDateText", "label")
    )
    date_match = re.search(
        r"(?:(\d{4})\s*[.년/-]\s*)?(\d{1,2})\s*[.월/-]\s*(\d{1,2})",
        rendered,
    )
    time_match = re.search(r"(오전|오후)?\s*(\d{1,2})\s*:\s*(\d{2})", rendered)
    if not date_match or not time_match:
        return False
    year_text, month_text, day_text = date_match.groups()
    meridiem, hour_text, minute_text = time_match.groups()
    hour = int(hour_text)
    if meridiem == "오후" and hour < 12:
        hour += 12
    elif meridiem == "오전" and hour == 12:
        hour = 0
    return (
        (not year_text or int(year_text) == target.year)
        and int(month_text) == target.month
        and int(day_text) == target.day
        and hour == target.hour
        and int(minute_text) == target.minute
    )


def match_upcoming_booking(
    rows: Any,
    *,
    target_date: str,
    target_time: str,
    business_id: str = "",
    item_name: str = "",
) -> NaverBookingReconciliation:
    """Require account booking id plus exact business/item/date/time evidence."""
    if not isinstance(rows, list):
        return NaverBookingReconciliation(False)
    expected_item = _compact_match_text(item_name)
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        booking_id = str(raw.get("id") or "").strip()
        if not booking_id:
            continue
        row_business_id = str(raw.get("businessId") or "").strip()
        if business_id and row_business_id != str(business_id):
            continue
        row_item = _compact_match_text(raw.get("bizItemName"))
        if expected_item:
            if not row_item or not (
                expected_item in row_item or row_item in expected_item
            ):
                continue
        if not _row_booking_datetime(raw, target_date, target_time):
            continue
        return NaverBookingReconciliation(
            True,
            booking_id=booking_id,
            url=str(raw.get("landingUrl") or ""),
            status=str(raw.get("bookingStatusCode") or ""),
        )
    return NaverBookingReconciliation(False)


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def _iso_millis_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _optional_positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


@dataclass(frozen=True)
class NaverBookingQuantity:
    count: int
    minimum: int
    maximum: int
    available: int | None
    quantity_mode: bool


def resolve_booking_quantity(
    slot: Mapping[str, Any], reservation: Mapping[str, Any]
) -> NaverBookingQuantity:
    """Distinguish one-slot reservations from per-person ticket quantities.

    Both ordinary escape rooms and immersive performances use Naver business
    type 12, so business type is not a useful discriminator.  The official page
    renders its +/- quantity control when a slot can sell multiple units: the
    explicit maximum exceeds one, or min/max are absent while unit stock exceeds
    one.  Fixed 1..1 slots remain a single reservation even when the form asks
    for several participants.
    """
    raw_minimum = _optional_positive_int(slot.get("minBookingCount"))
    raw_maximum = _optional_positive_int(slot.get("maxBookingCount"))
    unit_stock = _optional_positive_int(slot.get("unitStock"))
    unit_booked = _optional_positive_int(slot.get("unitBookingCount"))
    requested = max(1, int(_digits(reservation.get("people")) or 1))
    minimum = max(1, raw_minimum or 1)

    quantity_mode = bool(
        (raw_maximum is not None and raw_maximum > 1)
        or (
            raw_maximum is None
            and unit_stock is not None
            and unit_stock > 1
            and (raw_minimum is None or raw_minimum <= 1)
        )
    )
    if quantity_mode:
        maximum = max(minimum, raw_maximum or unit_stock or requested)
        count = max(minimum, requested)
    else:
        maximum = max(minimum, raw_maximum or minimum)
        count = minimum

    available = None
    if unit_stock is not None and unit_booked is not None:
        available = max(0, unit_stock - unit_booked)
    return NaverBookingQuantity(
        count=count,
        minimum=minimum,
        maximum=maximum,
        available=available,
        quantity_mode=quantity_mode,
    )


def _bool_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return copy.deepcopy(loaded) if isinstance(loaded, dict) else {}
    return {}


def _payment_profile(
    biz_item: Mapping[str, Any], slot: Mapping[str, Any]
) -> tuple[str, str, bool, bool, dict[str, Any]]:
    """Mirror the official request page's pre/post-payment decision.

    ``isNPayUsed`` only means that the merchant is connected to Npay.  The
    selected slot's ``isPostPayment`` field is authoritative.  Naver treats a
    successful Slot query returning null as false (immediate checkout), which is
    why the engine carries an explicit ``_isPostPaymentResolved`` marker.
    """
    is_npay_used = _bool_flag(biz_item.get("isNPayUsed"))
    payment_setting = _json_mapping(biz_item.get("paymentSettingJson"))
    if not is_npay_used:
        return PAYMENT_BOOKING, "bizItem.isNPayUsed=false", False, False, payment_setting

    if _bool_flag(slot.get("_isPostPaymentResolved")):
        is_post_payment = slot.get("isPostPayment") is True
        source = "slotSeat.isPostPayment"
    else:
        payment_moment = str(payment_setting.get("paymentMoment") or "").upper()
        is_post_payment = payment_moment == "POST"
        source = (
            "bizItem.paymentSettingJson.paymentMoment"
            if payment_moment
            else "네이버 기본값(선결제)"
        )

    mode = PAYMENT_POSTPAID if is_post_payment else PAYMENT_NPAY_PREPAID
    return mode, source, True, is_post_payment, payment_setting


def _first_resource(resources: Any) -> str | None:
    if not isinstance(resources, list) or not resources:
        return None
    first = resources[0] if isinstance(resources[0], dict) else {}
    return str(first.get("resourceUrl") or "") or None


class NaverSubmitPayloadBuilder:
    """Convert the public GraphQL records into the page's supported payload."""

    TERMS_VERSION = TERMS_VERSION

    def prepare(
        self,
        *,
        business: dict[str, Any],
        biz_item: dict[str, Any],
        slot: dict[str, Any],
        account: NaverAccount,
        reservation: Mapping[str, Any],
    ) -> NaverSubmitPreparation:
        if not account.is_logged_in or not account.csrf_token:
            return NaverSubmitPreparation(
                False, reason="네이버 로그인 또는 CSRF 확인에 실패했습니다"
            )
        business_type_id = int(business.get("businessTypeId") or 0)
        if business_type_id <= 0:
            return NaverSubmitPreparation(
                False, reason="상품 유형 정보를 확인하지 못했습니다"
            )
        if biz_item.get("isSeatUsed") or biz_item.get("isPeriodFixed"):
            return NaverSubmitPreparation(
                False,
                reason="좌석제·기간제 상품은 API 직접 제출을 지원하지 않습니다",
            )

        (
            payment_mode,
            payment_source,
            is_npay_used,
            is_post_payment,
            payment_setting,
        ) = _payment_profile(biz_item, slot)
        if (
            is_npay_used
            and is_post_payment
            and str(payment_setting.get("paymentMoment") or "").upper() == "POST"
            and not payment_setting.get("userSelectedPaymentMethod")
        ):
            return NaverSubmitPreparation(
                False,
                reason="후결제 결제수단이 아직 선택되지 않아 API 페이로드를 확정할 수 없습니다",
                payment_mode=payment_mode,
                payment_source=payment_source,
            )

        start = _parse_datetime(slot.get("unitStartDateTime"))
        if start is None:
            return NaverSubmitPreparation(
                False, reason="슬롯 예약 시각을 해석하지 못했습니다"
            )
        start_kst = start.astimezone(KST)
        wanted_date = str(reservation.get("reservationDate") or "")
        wanted_time = str(reservation.get("reservationTime") or "")[:5]
        if (
            start_kst.strftime("%Y-%m-%d") != wanted_date
            or start_kst.strftime("%H:%M") != wanted_time
        ):
            return NaverSubmitPreparation(
                False, reason="슬롯 예약 시각이 선택한 날짜·시간과 일치하지 않습니다"
            )

        name = str(reservation.get("name") or "").strip()
        phone = _digits(reservation.get("phone"))
        if not name or not phone:
            return NaverSubmitPreparation(
                False, reason="예약자 이름 또는 연락처가 비어 있습니다"
            )

        prices = [
            copy.deepcopy(price)
            for price in (slot.get("prices") or [])
            if isinstance(price, dict)
            and price.get("isImp", True) is not False
        ]
        if not prices:
            return NaverSubmitPreparation(
                False, reason="예약 가능한 가격 선택지가 없습니다"
            )
        quantity = resolve_booking_quantity(slot, reservation)
        booking_count = quantity.count
        if booking_count > quantity.maximum:
            return NaverSubmitPreparation(
                False,
                reason=(
                    f"예약 수량 {booking_count}개가 상품 최대 수량 "
                    f"{quantity.maximum}개를 초과합니다"
                ),
                quantity_mode=quantity.quantity_mode,
                available_count=quantity.available,
            )
        if quantity.available is not None and booking_count > quantity.available:
            return NaverSubmitPreparation(
                False,
                reason=(
                    f"예약 수량 {booking_count}개에 비해 현재 잔여 수량이 "
                    f"{quantity.available}개뿐입니다"
                ),
                quantity_mode=quantity.quantity_mode,
                available_count=quantity.available,
            )
        selected_price_index = next(
            (index for index, price in enumerate(prices) if price.get("isDefault")),
            0,
        )
        for index, price in enumerate(prices):
            price["bookingCount"] = booking_count if index == selected_price_index else 0
        base_price = int(prices[selected_price_index].get("price") or 0) * booking_count
        if base_price < 0:
            return NaverSubmitPreparation(
                False, reason="슬롯 가격이 올바르지 않습니다"
            )

        item_form = biz_item.get("customFormJson")
        form = (
            item_form
            if isinstance(item_form, list) and item_form
            else business.get("customFormJson")
        )
        custom_form, _answers, form_error = prepare_custom_form_answers(
            form,
            reservation,
            item_count=booking_count,
        )
        if form_error:
            return NaverSubmitPreparation(False, reason=form_error)

        raw_names = business.get("rawNames")
        raw_names = raw_names if isinstance(raw_names, dict) else {}
        agencies = business.get("agencies")
        agencies = agencies if isinstance(agencies, list) else []
        first_agency = agencies[0] if agencies and isinstance(agencies[0], dict) else {}
        refund_policy = business.get("refundPolicy")
        refund_policy = refund_policy if isinstance(refund_policy, dict) else {}
        start_iso = _iso_millis_utc(start)
        start_minute = start_kst.hour * 60 + start_kst.minute

        payload: dict[str, Any] = {
            "bookingId": None,
            "businessTypeId": business_type_id,
            "isNPayUsed": is_npay_used,
            "businessId": str(business.get("businessId") or ""),
            "businessName": str(
                raw_names.get("name") or business.get("name") or ""
            ),
            "serviceName": str(
                raw_names.get("serviceName") or business.get("serviceName") or ""
            ),
            "businessAddressJson": business.get("addressJson"),
            "bookingTimeUnitCode": business.get("bookingTimeUnitCode"),
            "translateStatusJson": business.get("translationStatusJson"),
            "bizItemId": str(biz_item.get("bizItemId") or ""),
            "bizItemName": str(biz_item.get("name") or ""),
            "isPeriodFixed": False,
            "bizItemAddressJson": biz_item.get("addressJson"),
            "isSeatUsed": False,
            "uncompletedBookingProcessCode": business.get(
                "uncompletedBookingProcessCode"
            ),
            "uncompletedBookingRefundRate": business.get(
                "uncompletedBookingRefundRate"
            ),
            "language": "ko",
            "userAgentJson": {
                "raw": "",
                "os": "",
                "os_version": "",
                "device": "PC",
            },
            "bookingCount": booking_count,
            "price": base_price,
            "priceTypeJson": prices,
            "name": name,
            "phone": phone,
            "email": str(reservation.get("email") or ""),
            "requestMessage": str(reservation.get("requestMessage") or ""),
            "customFormInputJson": custom_form,
            "termsVersion": self.TERMS_VERSION,
            "isSmsAlarm": account.is_sms_alarm,
            "csrfToken": account.csrf_token,
            "optionCategories": [],
            "isPostPayment": is_post_payment,
            "slotId": str(slot.get("slotId") or ""),
            "startMinute": start_minute,
            "endDate": start_kst.strftime("%Y-%m-%d"),
            "bizItemPrice": base_price,
            "slotName": str(slot.get("name") or ""),
            "visitorName": "",
            "visitorPhone": "",
            "hasVisitor": False,
            "paymentSettingJson": payment_setting or None,
            "extraFeeJson": {},
            "nPayRegStatusCode": business.get("nPayRegStatusCode"),
            "startDate": start_kst.strftime("%Y-%m-%d"),
            "hourBit": "",
            "startDateTime": start_iso,
            "endDateTime": start_iso,
            "endMinute": start_minute,
            "refundPolicyId": (
                str(refund_policy.get("refundPolicyId"))
                if refund_policy.get("refundPolicyId") is not None
                else None
            ),
            "businessThumbImage": _first_resource(
                business.get("businessResources")
            ),
            "agencyId": first_agency.get("agencyId"),
            "isAgency": bool(agencies),
            "bizItemThumbImage": _first_resource(biz_item.get("resources")),
            "bookingCondition": "",
            "bookingConfirmCode": (
                biz_item.get("bookingConfirmCode")
                or business.get("bookingConfirmCode")
            ),
            "naverPayBackUrl": "",
            "isAdminBooking": False,
            "globalTimezone": "Asia/Seoul",
            "bookingOptionJson": [],
            "birthday": None,
            "todayDealRate": None,
        }

        required = (
            "businessId",
            "bizItemId",
            "slotId",
            "csrfToken",
            "startDateTime",
            "name",
            "phone",
            "bookingConfirmCode",
        )
        missing = [key for key in required if not payload.get(key)]
        if missing:
            return NaverSubmitPreparation(
                False,
                reason="필수 제출 필드 누락: " + ", ".join(missing),
            )
        return NaverSubmitPreparation(
            True,
            payload=payload,
            slot_id=str(payload["slotId"]),
            payment_mode=payment_mode,
            payment_source=payment_source,
            quantity_mode=quantity.quantity_mode,
            available_count=quantity.available,
        )


BROWSER_GRAPHQL_SCRIPT = r"""async request => {
    const variables = request.variables || {};
    if (request.operationName === "submitBooking" &&
            variables.input && variables.input.userAgentJson) {
        const ua = navigator.userAgent || "";
        let os = (navigator.userAgentData &&
            navigator.userAgentData.platform) || navigator.platform || "";
        let osVersion = "";
        let device = "none";
        const windows = ua.match(/Windows NT ([0-9.]+)/i);
        const android = ua.match(/Android\s+([0-9.]+)/i);
        const ios = ua.match(/(?:iPhone OS|CPU OS)\s+([0-9_]+)/i);
        const mac = ua.match(/Mac OS X\s+([0-9_]+)/i);
        if (windows) {
            os = "Windows";
            osVersion = windows[1] === "10.0" ? "10" : windows[1];
        } else if (android) {
            os = "Android";
            osVersion = android[1];
        } else if (ios) {
            os = "iOS";
            osVersion = ios[1].replace(/_/g, ".");
            device = /iPad/i.test(ua) ? "iPad" : "iPhone";
        } else if (mac) {
            os = "Mac OS";
            osVersion = mac[1].replace(/_/g, ".");
        }
        variables.input.userAgentJson = {
            ...variables.input.userAgentJson,
            raw: ua,
            os,
            os_version: osVersion,
            device,
        };
    }
    const controller = new AbortController();
    const timeoutMs = Math.max(100, Number(request.timeoutMs) || 3000);
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const endpoint = "/graphql?opName=" +
            encodeURIComponent(request.operationName || "");
        const response = await fetch(endpoint, {
            method: "POST",
            credentials: "include",
            signal: controller.signal,
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                operationName: request.operationName,
                query: request.query,
                variables,
            }),
        });
        let body = null;
        try {
            body = await response.json();
        } catch (_) {
            body = null;
        }
        return {status: response.status, body};
    } finally {
        clearTimeout(timeout);
    }
}"""


BROWSER_UPCOMING_BOOKINGS_SCRIPT = r"""async request => {
    const controller = new AbortController();
    const timeout = setTimeout(
        () => controller.abort(), Math.max(300, Number(request.timeoutMs) || 2000));
    try {
        const response = await fetch(request.endpoint, {
            method: "POST",
            credentials: "include",
            signal: controller.signal,
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                operationName: "UpcomingBookingQuery",
                query: request.query,
                variables: {limit: 10},
            }),
        });
        let body = null;
        try { body = await response.json(); } catch (_) {}
        return {status: response.status, body};
    } finally {
        clearTimeout(timeout);
    }
}"""


# This is intentionally a single, pre-armed request.  It is installed while the
# page is idle several seconds before the published opening time, then Chrome's
# own event loop starts the same GraphQL mutation at the calculated moment.  That
# removes the last Python -> CDP scheduling hop without multiplying requests.
BROWSER_ARMED_SUBMIT_SCRIPT = r"""async request => {
    const stateKey = "__pengucroNaverArmedSubmit";
    const existing = window[stateKey];
    if (existing && (existing.status === "armed" || existing.status === "submitting")) {
        return {error: "active"};
    }
    const input = structuredClone(request.input || {});
    if (input.userAgentJson) {
        const ua = navigator.userAgent || "";
        let os = (navigator.userAgentData && navigator.userAgentData.platform) ||
            navigator.platform || "";
        let osVersion = "";
        let device = "none";
        const windows = ua.match(/Windows NT ([0-9.]+)/i);
        const android = ua.match(/Android\s+([0-9.]+)/i);
        const ios = ua.match(/(?:iPhone OS|CPU OS)\s+([0-9_]+)/i);
        const mac = ua.match(/Mac OS X\s+([0-9_]+)/i);
        if (windows) {
            os = "Windows";
            osVersion = windows[1] === "10.0" ? "10" : windows[1];
        } else if (android) {
            os = "Android";
            osVersion = android[1];
        } else if (ios) {
            os = "iOS";
            osVersion = ios[1].replace(/_/g, ".");
            device = /iPad/i.test(ua) ? "iPad" : "iPhone";
        } else if (mac) {
            os = "Mac OS";
            osVersion = mac[1].replace(/_/g, ".");
        }
        input.userAgentJson = {
            ...input.userAgentJson,
            raw: ua,
            os,
            os_version: osVersion,
            device,
        };
    }
    const delayMs = Math.max(0, Number(request.delayMs) || 0);
    const timeoutMs = Math.max(100, Number(request.timeoutMs) || 3000);
    const state = {
        id: String(request.armId || ""),
        status: "armed",
        armedAt: performance.now(),
        dueAt: performance.now() + delayMs,
        startedAt: 0,
        completedAt: 0,
        response: null,
        error: "",
        timer: 0,
        controller: null,
    };
    window[stateKey] = state;
    const pause = ms => new Promise(resolve => setTimeout(resolve, Math.max(0, ms)));
    const run = async () => {
        try {
            // Timer wakeups can be a few milliseconds late.  Keep the final
            // spin tiny so the booking page remains responsive.
            const quietUntil = state.dueAt - 6;
            if (performance.now() < quietUntil) {
                await pause(quietUntil - performance.now());
            }
            while (performance.now() < state.dueAt) {}
            if (state.status !== "armed") return;
            state.status = "submitting";
            state.startedAt = performance.now();
            const controller = new AbortController();
            state.controller = controller;
            const timeout = setTimeout(() => controller.abort(), timeoutMs);
            try {
                const response = await fetch("/graphql?opName=submitBooking", {
                    method: "POST",
                    credentials: "include",
                    signal: controller.signal,
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        operationName: "submitBooking",
                        query: request.query,
                        variables: {input},
                    }),
                });
                let body = null;
                try { body = await response.json(); } catch (_) {}
                state.response = {status: response.status, body};
            } finally {
                clearTimeout(timeout);
                state.controller = null;
            }
            state.status = "complete";
        } catch (error) {
            state.error = String((error && error.name) || "browser-fetch-error");
            state.status = "error";
        } finally {
            state.completedAt = performance.now();
        }
    };
    state.timer = setTimeout(() => { void run(); }, Math.max(0, delayMs - 8));
    return {id: state.id, status: state.status, delayMs};
}"""


BROWSER_ARMED_SUBMIT_STATE_SCRIPT = r"""request => {
    const state = window.__pengucroNaverArmedSubmit;
    if (!state || state.id !== String(request.armId || "")) return {status: "missing"};
    return {
        status: state.status,
        response: state.response,
        error: state.error || "",
        armedAt: Number(state.armedAt) || 0,
        dueAt: Number(state.dueAt) || 0,
        startedAt: Number(state.startedAt) || 0,
        completedAt: Number(state.completedAt) || 0,
    };
}"""


BROWSER_CANCEL_ARMED_SUBMIT_SCRIPT = r"""request => {
    const state = window.__pengucroNaverArmedSubmit;
    if (!state || state.id !== String(request.armId || "")) return false;
    if (state.status !== "armed") return false;
    clearTimeout(state.timer);
    state.status = "cancelled";
    state.completedAt = performance.now();
    return true;
}"""


_SENSITIVE_PAYLOAD_KEYS = {
    "csrftoken",
    "phone",
    "name",
    "email",
    "requestmessage",
    "customforminputjson",
    "visitorname",
    "visitorphone",
}


def _redact_payload_values(text: str, payload: Mapping[str, Any]) -> str:
    """Remove user/session values if a server error happens to echo them."""
    secrets: set[str] = set()

    def collect(value: Any, sensitive: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                collect(
                    nested,
                    sensitive or str(key).lower() in _SENSITIVE_PAYLOAD_KEYS,
                )
        elif isinstance(value, list):
            for nested in value:
                collect(nested, sensitive)
        elif sensitive:
            candidate = str(value or "")
            if len(candidate) >= 2:
                secrets.add(candidate)

    collect(payload)
    clean = text
    for secret in sorted(secrets, key=len, reverse=True):
        clean = clean.replace(secret, "[redacted]")
        digits = re.sub(r"\D", "", secret)
        if len(digits) >= 8:
            separated = r"[-.\s]*".join(re.escape(char) for char in digits)
            clean = re.sub(separated, "[redacted]", clean)
    return clean


def _submit_result_from_response(response: Any) -> SubmitResult:
    if not isinstance(response, dict):
        return SubmitResult(
            SubmitOutcome.ERROR, message="브라우저 GraphQL 응답 형식 오류"
        )
    status = int(response.get("status") or 0)
    body = response.get("body")
    if not isinstance(body, dict):
        detail = f"HTTP {status}" if status else "빈 응답"
        return SubmitResult(
            SubmitOutcome.ERROR,
            message=f"브라우저 GraphQL 응답을 해석하지 못했습니다 ({detail})",
        )

    # GraphQL may return partial data together with an error.  A concrete
    # bookingId is stronger evidence than RT47's generic wrapper and must not be
    # discarded, otherwise a real Npay hold is mistaken for somebody else's win.
    booking = ((body.get("data") or {}).get("submitBooking")) or {}
    booking_id = str(booking.get("bookingId") or "")
    if booking_id:
        return SubmitResult(
            SubmitOutcome.SUCCESS,
            booking_id=booking_id,
            url=booking.get("url"),
        )

    errors = body.get("errors") or []
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        extensions = first.get("extensions")
        extensions = extensions if isinstance(extensions, dict) else {}
        code = str(extensions.get("code") or "")
        message = str(first.get("message") or "")
        reason = str(extensions.get("reason") or "")
        return SubmitResult(
            classify_submit_error(code, message, reason),
            code=code or message,
            message=reason or message,
        )

    if status >= 400:
        return SubmitResult(SubmitOutcome.ERROR, message=f"HTTP {status}")
    return SubmitResult(SubmitOutcome.ERROR, message="예약번호가 비어 있습니다")


class NaverBrowserSubmitter:
    """Execute GraphQL inside the already-authenticated Naver page."""

    def __init__(self, page, timeout_seconds: float = 3.0) -> None:
        self.page = page
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self.last_rtt: float | None = None
        self.safe_rtt_samples: list[float] = []
        self.last_armed_timing: dict[str, float] = {}
        # ``False`` distinguishes a failed GraphQL request from a successful
        # response that explicitly says the current browser is logged out.
        self.last_account_fetch_ok = False

    async def _graphql(
        self, operation_name: str, query: str, variables: dict[str, Any]
    ) -> Any:
        started = time.monotonic()
        try:
            return await asyncio.wait_for(
                self.page.evaluate(
                    BROWSER_GRAPHQL_SCRIPT,
                    {
                        "operationName": operation_name,
                        "query": query,
                        "variables": variables,
                        "timeoutMs": round(self.timeout_seconds * 1000),
                    },
                ),
                timeout=self.timeout_seconds + 0.25,
            )
        finally:
            elapsed = time.monotonic() - started
            self.last_rtt = elapsed
            if operation_name != "submitBooking" and 0.005 <= elapsed <= 3.0:
                self.safe_rtt_samples.append(elapsed)
                del self.safe_rtt_samples[:-8]

    async def fetch_account(self) -> NaverAccount:
        self.last_account_fetch_ok = False
        try:
            response = await self._graphql("account", ACCOUNT_QUERY, {})
        except Exception:
            return NaverAccount(False, "", False)
        body = response.get("body") if isinstance(response, dict) else None
        data = body.get("data") if isinstance(body, dict) else None
        account = data.get("account") if isinstance(data, dict) else None
        if not isinstance(account, dict):
            return NaverAccount(False, "", False)
        self.last_account_fetch_ok = True
        return NaverAccount(
            is_logged_in=bool(account.get("isLoggedIn")),
            csrf_token=str(account.get("csrfToken") or ""),
            is_sms_alarm=bool(account.get("isSmsAlarm")),
            user_id=str(account.get("userId") or ""),
            nickname=str(account.get("nickname") or ""),
        )

    async def reconcile_upcoming_booking(
        self,
        *,
        target_date: str,
        target_time: str,
        business_id: str = "",
        item_name: str = "",
        attempts: int = 4,
    ) -> NaverBookingReconciliation:
        """Read the logged-in account's booking list without another mutation."""
        context = getattr(self.page, "context", None)
        if context is None or not hasattr(context, "new_page"):
            return NaverBookingReconciliation(False)
        lookup_page = None
        try:
            lookup_page = await context.new_page()
            await lookup_page.goto(
                UPCOMING_BOOKINGS_URL,
                wait_until="domcontentloaded",
                timeout=5000,
            )
            for attempt in range(max(1, min(int(attempts or 1), 6))):
                response = await asyncio.wait_for(
                    lookup_page.evaluate(
                        BROWSER_UPCOMING_BOOKINGS_SCRIPT,
                        {
                            "endpoint": UPCOMING_BOOKINGS_ENDPOINT,
                            "query": UPCOMING_BOOKINGS_QUERY,
                            "timeoutMs": 2000,
                        },
                    ),
                    timeout=2.5,
                )
                body = response.get("body") if isinstance(response, dict) else None
                data = body.get("data") if isinstance(body, dict) else None
                me = data.get("me") if isinstance(data, dict) else None
                upcoming = (
                    me.get("upcomingBookings") if isinstance(me, dict) else None
                )
                rows = upcoming.get("bookings") if isinstance(upcoming, dict) else []
                matched = match_upcoming_booking(
                    rows,
                    target_date=target_date,
                    target_time=target_time,
                    business_id=business_id,
                    item_name=item_name,
                )
                if matched.found:
                    return matched
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.15)
        except Exception:
            return NaverBookingReconciliation(False)
        finally:
            if lookup_page is not None:
                try:
                    await lookup_page.close()
                except Exception:
                    pass
        return NaverBookingReconciliation(False)

    async def submit(self, payload: dict[str, Any]) -> SubmitResult:
        try:
            response = await self._graphql(
                "submitBooking",
                SUBMIT_BOOKING_MUTATION,
                {"input": copy.deepcopy(payload)},
            )
        except (asyncio.TimeoutError, TimeoutError):
            return SubmitResult(
                SubmitOutcome.UNKNOWN,
                message="전송 결과가 불명확합니다. 네이버 예약 내역을 확인해주세요.",
            )
        except Exception as exc:
            return SubmitResult(
                SubmitOutcome.UNKNOWN,
                message=(
                    "브라우저 GraphQL 전송 결과가 불명확합니다. "
                    f"예약 내역을 확인해주세요. ({type(exc).__name__})"
                ),
            )
        result = _submit_result_from_response(response)
        return SubmitResult(
            result.outcome,
            code=_redact_payload_values(result.code, payload),
            message=_redact_payload_values(result.message, payload),
            booking_id=result.booking_id,
            url=result.url,
        )

    async def arm_submit_at(self, payload: dict[str, Any], delay_seconds: float) -> str:
        """Arm exactly one browser-internal submit and return its opaque id."""
        arm_id = f"naver-{time.monotonic_ns()}"
        try:
            response = await asyncio.wait_for(
                self.page.evaluate(
                    BROWSER_ARMED_SUBMIT_SCRIPT,
                    {
                        "armId": arm_id,
                        "input": copy.deepcopy(payload),
                        "query": SUBMIT_BOOKING_MUTATION,
                        "delayMs": round(max(0.0, float(delay_seconds)) * 1000),
                        "timeoutMs": round(self.timeout_seconds * 1000),
                    },
                ),
                timeout=1.5,
            )
        except Exception as exc:
            raise RuntimeError("브라우저 내부 예약 타이머를 준비하지 못했습니다") from exc
        if not isinstance(response, dict) or response.get("id") != arm_id:
            raise RuntimeError("브라우저 내부 예약 타이머 응답이 올바르지 않습니다")
        return arm_id

    async def read_armed_submit(self, arm_id: str, payload: Mapping[str, Any]) -> tuple[str, SubmitResult | None, float | None]:
        """Read one armed request without sending another booking mutation."""
        try:
            state = await self.page.evaluate(
                BROWSER_ARMED_SUBMIT_STATE_SCRIPT, {"armId": arm_id}
            )
        except Exception:
            return "error", SubmitResult(
                SubmitOutcome.UNKNOWN,
                message="브라우저 내부 예약 결과를 읽지 못했습니다. 예약내역을 확인해주세요.",
            ), None
        if not isinstance(state, dict):
            return "error", SubmitResult(
                SubmitOutcome.UNKNOWN,
                message="브라우저 내부 예약 상태 형식이 올바르지 않습니다.",
            ), None
        status = str(state.get("status") or "error")
        timing = {}
        for key in ("armedAt", "dueAt", "startedAt", "completedAt"):
            value = state.get(key)
            if isinstance(value, (int, float)):
                timing[key] = float(value)
        self.last_armed_timing = timing
        elapsed_ms: float | None = None
        started = state.get("startedAt")
        completed = state.get("completedAt")
        if isinstance(started, (int, float)) and isinstance(completed, (int, float)) and completed >= started > 0:
            elapsed_ms = float(completed - started)
            self.last_rtt = elapsed_ms / 1000
        if status == "complete":
            result = _submit_result_from_response(state.get("response"))
            return status, SubmitResult(
                result.outcome,
                code=_redact_payload_values(result.code, payload),
                message=_redact_payload_values(result.message, payload),
                booking_id=result.booking_id,
                url=result.url,
            ), elapsed_ms
        if status in {"error", "missing"}:
            return status, SubmitResult(
                SubmitOutcome.UNKNOWN,
                message="브라우저 내부 예약 전송 결과를 확인할 수 없습니다. 예약내역을 확인해주세요.",
            ), elapsed_ms
        if status == "cancelled":
            return status, None, elapsed_ms
        return status, None, elapsed_ms

    async def cancel_armed_submit(self, arm_id: str) -> bool:
        try:
            return bool(await self.page.evaluate(
                BROWSER_CANCEL_ARMED_SUBMIT_SCRIPT, {"armId": arm_id}
            ))
        except Exception:
            return False
