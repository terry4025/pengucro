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
import math
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

from engines.naver_api import (
    ACCOUNT_QUERY,
    KST,
    SUBMIT_BOOKING_MUTATION,
    SUBMIT_NOT_OPEN_CODES,
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
NOT_OPEN_WRAPPER_CODES = frozenset({"BAD_REQUEST", "BAD_USER_INPUT"})


UPCOMING_BOOKINGS_QUERY = """query UpcomingBookingQuery($page: Int, $limit: Int) {
  me {
    __typename
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
    state: str = "not_found"
    attempts: int = 0
    successful_reads: int = 0
    failed_reads: int = 0
    http_status: int = 0
    error_kind: str = ""
    baseline_checked: bool = False

    def __post_init__(self) -> None:
        if self.found and self.state == "not_found":
            object.__setattr__(self, "state", "found")


@dataclass(frozen=True)
class NaverBookingSnapshot:
    """A classified account read; an error is never an empty booking list."""

    state: str
    rows: tuple[Mapping[str, Any], ...] = ()
    booking_ids: frozenset[str] = frozenset()
    complete: bool = False
    pages_checked: int = 0
    http_status: int = 0
    error_kind: str = ""
    has_more: bool = False


# The official MY Place bundle (260903-151652/5.37.0, chunk 6629) distinguishes
# RC03 confirmation from RC02 pending confirmation. Both identify an active
# account booking, but RC02 must never be presented as final confirmation.
VALID_BOOKING_STATUSES = frozenset({"RC02", "RC03"})


def trusted_booking_evidence_url(raw_url: Any) -> str:
    """Keep only an official HTTPS checkout/detail URL as unconfirmed evidence."""
    candidates = (
        (raw_url.get("pc"), raw_url.get("mobile"), raw_url.get("m"))
        if isinstance(raw_url, Mapping) else (raw_url,)
    )
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text.startswith("/") and not text.startswith("//"):
            text = urljoin("https://m.booking.naver.com", text)
        try:
            parsed = urlparse(text)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or parsed.username or parsed.password:
                continue
            if parsed.port not in (None, 443):
                continue
        except (TypeError, ValueError):
            continue
        if host == "pay.naver.com" or host.endswith(".pay.naver.com"):
            return text
        if (host == "booking.naver.com" or host.endswith(".booking.naver.com")) and (
            parsed.path.startswith("/my/bookings/")
        ):
            return text
    return ""


def _submit_error_tokens(error: Mapping[str, Any]) -> tuple[str, ...]:
    extensions = error.get("extensions")
    extensions = extensions if isinstance(extensions, Mapping) else {}
    return tuple(
        str(value).strip() for value in (
            error.get("message"), extensions.get("code"), extensions.get("reason")
        ) if value is not None and str(value).strip()
    )


def _exclusive_not_open_errors(errors: Any) -> bool:
    """Only unopposed, explicit not-open evidence can authorize another POST."""
    if not isinstance(errors, list) or not errors:
        return False
    allowed = SUBMIT_NOT_OPEN_CODES | NOT_OPEN_WRAPPER_CODES
    for error in errors:
        if not isinstance(error, Mapping):
            return False
        extensions = error.get("extensions")
        if extensions is not None and not isinstance(extensions, Mapping):
            return False
        values = set(_submit_error_tokens(error))
        if not values.intersection(SUBMIT_NOT_OPEN_CODES) or not values.issubset(allowed):
            return False
    return True


def _booking_snapshot(response: Any) -> NaverBookingSnapshot:
    if not isinstance(response, Mapping):
        return NaverBookingSnapshot("network_error", error_kind="missing_response")
    try:
        status = int(response.get("status") or 0)
    except (ValueError, TypeError):
        status = 0
    if status in (401, 403):
        return NaverBookingSnapshot("auth_error", http_status=status, error_kind="http_auth")
    if status >= 400 or status <= 0:
        return NaverBookingSnapshot("http_error", http_status=status, error_kind="http_status")
    body = response.get("body")
    if not isinstance(body, Mapping):
        return NaverBookingSnapshot("schema_error", http_status=status, error_kind="non_json_body")
    errors = body.get("errors") or []
    if errors:
        # Inspect errors for classification, but never retain messages that may
        # echo account details or session data in the diagnostic result.
        text = json.dumps(errors, ensure_ascii=False).casefold()
        if any(token in text for token in ("unauthenticated", "authentication", "unauthorized", "not logged")):
            state, kind = "auth_error", "graphql_auth"
        elif any(token in text for token in ("cannot query field", "validation", "syntax error", "unknown argument")):
            state, kind = "schema_error", "graphql_schema"
        else:
            state, kind = "query_error", "graphql_error"
        return NaverBookingSnapshot(state, http_status=status, error_kind=kind)
    data = body.get("data")
    if not isinstance(data, Mapping) or "me" not in data:
        return NaverBookingSnapshot("schema_error", http_status=status, error_kind="missing_account_field")
    me = data.get("me")
    if me is None:
        return NaverBookingSnapshot("auth_error", http_status=status, error_kind="account_unavailable")
    if not isinstance(me, Mapping):
        return NaverBookingSnapshot("schema_error", http_status=status, error_kind="invalid_account")
    typename = str(me.get("__typename") or "")
    if typename and typename != "MeSucceed":
        return NaverBookingSnapshot("auth_error", http_status=status, error_kind="account_not_succeeded")
    upcoming = me.get("upcomingBookings")
    if not isinstance(upcoming, Mapping) or not isinstance(upcoming.get("bookings"), list):
        return NaverBookingSnapshot("schema_error", http_status=status, error_kind="missing_booking_list")
    rows = upcoming["bookings"]
    if any(not isinstance(row, Mapping) for row in rows):
        return NaverBookingSnapshot("schema_error", http_status=status, error_kind="invalid_booking_row")
    if any(not str(row.get("id") or "").strip() for row in rows):
        return NaverBookingSnapshot("schema_error", http_status=status, error_kind="missing_booking_id")
    page_info = upcoming.get("pageInfo")
    has_more = bool(page_info.get("hasNextPage")) if isinstance(page_info, Mapping) else False
    complete = isinstance(page_info, Mapping) and page_info.get("hasNextPage") is False
    return NaverBookingSnapshot(
        "ok", rows=tuple(rows),
        booking_ids=frozenset(str(row.get("id")) for row in rows if row.get("id")),
        complete=complete, pages_checked=1, http_status=status, has_more=has_more,
    )


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
    baseline_booking_ids: frozenset[str] | set[str] | None = None,
) -> NaverBookingReconciliation:
    """Require account booking id plus exact business/item/date/time evidence."""
    if not isinstance(rows, (list, tuple)):
        return NaverBookingReconciliation(False)
    expected_item = _compact_match_text(item_name)
    candidates = []
    rejected = None
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
            if not row_item or row_item != expected_item:
                continue
        if not _row_booking_datetime(raw, target_date, target_time):
            continue
        status = str(raw.get("bookingStatusCode") or "")
        existing = baseline_booking_ids is not None and booking_id in baseline_booking_ids
        evidence = NaverBookingReconciliation(
            status in VALID_BOOKING_STATUSES and not existing,
            booking_id=booking_id,
            url=trusted_booking_evidence_url(raw.get("landingUrl")),
            status=status,
            state="invalid_status" if status not in VALID_BOOKING_STATUSES else "existing" if existing else "found",
            baseline_checked=baseline_booking_ids is not None,
        )
        if evidence.found:
            candidates.append(evidence)
        else:
            rejected = evidence
    unique = {candidate.booking_id: candidate for candidate in candidates}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        return NaverBookingReconciliation(False, state="ambiguous_match", baseline_checked=baseline_booking_ids is not None)
    return rejected or NaverBookingReconciliation(False, baseline_checked=baseline_booking_ids is not None)


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
    const startedAt = performance.now();
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
        return {
            status: response.status,
            body,
            elapsedMs: performance.now() - startedAt,
        };
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
                variables: {
                    page: Math.max(1, Number(request.page) || 1),
                    limit: Math.max(10, Math.min(30, Number(request.limit) || 20)),
                },
            }),
        });
        let body = null;
        try { body = await response.json(); } catch (_) {}
        return {status: response.status, body};
    } finally {
        clearTimeout(timeout);
    }
}"""


# This is one pre-armed reservation flow. It is installed while the page is idle
# several seconds before the published opening time, then Chrome's own event loop
# starts the GraphQL mutation at the calculated moment. A retry is permitted only
# after Naver explicitly says the item is not open; every ambiguous or potentially
# successful response stops the flow immediately.
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
    // Serialize while the page is idle. JSON.stringify and object traversal do
    // not belong in the final millisecond before a one-seat opening.
    const requestBody = JSON.stringify({
        operationName: "submitBooking",
        query: request.query,
        variables: {input},
    });
    const timeoutMs = Math.max(100, Number(request.timeoutMs) || 3000);
    const maxAttempts = Math.max(1, Math.min(3, Number(request.maxAttempts) || 1));
    const retryDelayMs = Math.max(0, Number(request.retryDelayMs) || 0);
    const retryLeadMs = Math.max(0, Number(request.retryLeadMs) || 0);
    const retryWindowMs = Math.max(
        0, Math.min(500, Number(request.retryWindowMs) || 0));
    const notOpenCodes = new Set(
        (request.notOpenCodes || []).map(value => String(value || "")));
    const notOpenWrappers = new Set(
        (request.notOpenWrapperCodes || []).map(value => String(value || "")));
    const visibility = () => typeof document === "object"
        ? String(document.visibilityState || "unknown") : "unknown";

    // The synchronized Naver clock is the sole opening-time authority. Only
    // transfer its monotonic deadline to Chrome; do not replace it with a new
    // batch of independent server samples just before submission.
    if (Number(request.clockOrigin) !== performance.timeOrigin) {
        return {error: "clock-context-changed"};
    }
    const serverOpenAt = Number(request.serverOpenAtPerfMs) || 0;
    const clockRttMs = 0;
    const clockSampleCount = 0;
    const clockBridgeRttMs = Number(request.clockBridgeRttMs) || 0;
    const clockUncertaintyMs = Number(request.clockUncertaintyMs) || 0;
    const clockSpreadMs = 0;
    const estimatedOutboundMs = retryLeadMs;
    const armedAt = performance.now();
    const targetArrivalBeforeOpenMs = Number(request.targetArrivalBeforeOpenMs) || 0;
    const appliedLeadMs = Math.max(0, Number(request.leadMs) || 0);
    const dueAt = Math.max(armedAt, Number(request.dueAtPerfMs) || armedAt);
    const state = {
        id: String(request.armId || ""),
        status: "armed",
        armedAt,
        dueAt,
        serverOpenAt,
        clockRttMs,
        clockBridgeRttMs,
        clockSampleCount,
        clockUncertaintyMs,
        clockSpreadMs,
        estimatedOutboundMs,
        targetArrivalBeforeOpenMs,
        appliedLeadMs,
        startedAt: 0,
        lastStartedAt: 0,
        headersAt: 0,
        responseBodyAt: 0,
        completedAt: 0,
        attempts: 0,
        notOpenAttempts: 0,
        response: null,
        attemptTimings: [],
        armedVisibility: visibility(),
        dispatchVisibility: "unknown",
        error: "",
        timer: 0,
        controller: null,
    };
    window[stateKey] = state;
    const pause = ms => new Promise(resolve => setTimeout(resolve, Math.max(0, ms)));
    const isExplicitNotOpen = body => {
        const booking = body && body.data && body.data.submitBooking;
        // Partial data can identify a hold even when the sibling id is absent.
        // Preserve it for reconciliation, never retry it as a not-open refusal.
        if (booking && (booking.bookingId || booking.url)) return false;
        const errors = body && Array.isArray(body.errors) ? body.errors : [];
        return errors.length > 0 && errors.every(error => {
            if (!error || typeof error !== "object") return false;
            if (error.extensions != null && typeof error.extensions !== "object") return false;
            const extensions = error.extensions && typeof error.extensions === "object"
                ? error.extensions : {};
            const values = [error.message, extensions.code, extensions.reason]
                .filter(value => value != null).map(value => String(value).trim())
                .filter(Boolean);
            return values.some(value => notOpenCodes.has(value)) &&
                values.every(value => notOpenCodes.has(value) || notOpenWrappers.has(value));
        });
    };
    const retryExpired = () => state.serverOpenAt > 0
        ? performance.now() > state.serverOpenAt + retryWindowMs
        : performance.now() - state.startedAt > retryWindowMs;
    const run = async () => {
        try {
            // Timer wakeups can be a few milliseconds late. Keep the final spin
            // tiny so the booking page remains responsive.
            const quietUntil = state.dueAt - 6;
            if (performance.now() < quietUntil) {
                await pause(quietUntil - performance.now());
            }
            while (performance.now() < state.dueAt) {}
            if (state.status !== "armed") return;
            state.status = "submitting";
            state.dispatchVisibility = visibility();
            state.startedAt = performance.now();
            for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
                if (attempt > 1 && retryExpired()) break;
                state.attempts = attempt;
                state.lastStartedAt = performance.now();
                const attemptTiming = {attempt, dispatchAt: state.lastStartedAt};
                const controller = new AbortController();
                state.controller = controller;
                const timeout = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const response = await fetch("/graphql?opName=submitBooking", {
                        method: "POST",
                        credentials: "include",
                        signal: controller.signal,
                        headers: {"Content-Type": "application/json"},
                        body: requestBody,
                    });
                    state.headersAt = performance.now();
                    attemptTiming.headersAt = state.headersAt;
                    let body = null;
                    try { body = await response.json(); } catch (_) {}
                    state.responseBodyAt = performance.now();
                    attemptTiming.bodyAt = state.responseBodyAt;
                    state.response = {status: response.status, body};
                    const explicitNotOpen = isExplicitNotOpen(body);
                    if (explicitNotOpen) state.notOpenAttempts += 1;
                    if (!explicitNotOpen || attempt >= maxAttempts) break;
                    if (retryExpired()) break;
                } finally {
                    clearTimeout(timeout);
                    state.controller = null;
                    attemptTiming.completedAt = performance.now();
                    // Optional diagnostics after the response, never another
                    // network call or a delay in the final dispatch path.
                    try {
                        if (typeof performance.getEntriesByType === "function") {
                            const entries = performance.getEntriesByType("resource");
                            const entry = entries.filter(value =>
                                value.name.includes("opName=submitBooking") &&
                                value.startTime >= attemptTiming.dispatchAt - 1
                            ).pop();
                            if (entry) {
                                for (const key of ["fetchStart", "requestStart", "responseStart", "responseEnd"]) {
                                    const value = Number(entry[key]);
                                    if (Number.isFinite(value) && value > 0) attemptTiming[key] = value;
                                }
                            }
                        }
                    } catch (_) {}
                    state.attemptTimings.push(attemptTiming);
                }
                if (state.serverOpenAt > 0) {
                    // After a deliberate early probe, align the retry with the
                    // actual gate instead of spending all attempts before open.
                    const boundarySendAt = state.serverOpenAt - (
                        state.estimatedOutboundMs > 0
                            ? state.estimatedOutboundMs
                            : retryLeadMs);
                    const waitMs = boundarySendAt - performance.now();
                    if (waitMs > 0) await pause(waitMs);
                } else {
                    await pause(retryDelayMs);
                }
            }
            state.status = "complete";
        } catch (error) {
            state.error = String((error && error.name) || "browser-fetch-error");
            state.status = "error";
        } finally {
            state.completedAt = performance.now();
        }
    };
    state.timer = setTimeout(
        () => { void run(); }, Math.max(0, state.dueAt - performance.now() - 8));
    return {
        id: state.id,
        status: state.status,
        delayMs: Math.max(0, state.dueAt - performance.now()),
        armedAt: state.armedAt,
        dueAt: state.dueAt,
        serverOpenAt: state.serverOpenAt,
        clockRttMs: state.clockRttMs,
        clockBridgeRttMs: state.clockBridgeRttMs,
        clockSampleCount: state.clockSampleCount,
        clockUncertaintyMs: state.clockUncertaintyMs,
        clockSpreadMs: state.clockSpreadMs,
        estimatedOutboundMs: state.estimatedOutboundMs,
        targetArrivalBeforeOpenMs: state.targetArrivalBeforeOpenMs,
        appliedLeadMs: state.appliedLeadMs,
        armedVisibility: state.armedVisibility,
    };
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
        serverOpenAt: Number(state.serverOpenAt) || 0,
        clockRttMs: Number(state.clockRttMs) || 0,
        clockBridgeRttMs: Number(state.clockBridgeRttMs) || 0,
        clockSampleCount: Number(state.clockSampleCount) || 0,
        clockUncertaintyMs: Number(state.clockUncertaintyMs) || 0,
        clockSpreadMs: Number(state.clockSpreadMs) || 0,
        estimatedOutboundMs: Number(state.estimatedOutboundMs) || 0,
        targetArrivalBeforeOpenMs: Number(state.targetArrivalBeforeOpenMs) || 0,
        appliedLeadMs: Number(state.appliedLeadMs) || 0,
        startedAt: Number(state.startedAt) || 0,
        lastStartedAt: Number(state.lastStartedAt) || 0,
        headersAt: Number(state.headersAt) || 0,
        responseBodyAt: Number(state.responseBodyAt) || 0,
        completedAt: Number(state.completedAt) || 0,
        attempts: Number(state.attempts) || 0,
        notOpenAttempts: Number(state.notOpenAttempts) || 0,
        attemptTimings: Array.isArray(state.attemptTimings) ? state.attemptTimings : [],
        armedVisibility: state.armedVisibility || "unknown",
        dispatchVisibility: state.dispatchVisibility || "unknown",
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
    data = body.get("data")
    booking = data.get("submitBooking") if isinstance(data, Mapping) else None
    booking = booking if isinstance(booking, Mapping) else {}
    booking_url = booking.get("url") if isinstance(booking, dict) else None
    booking_url_text = trusted_booking_evidence_url(booking_url)
    booking_id = str(booking.get("bookingId") or "") if isinstance(booking, dict) else ""
    # A URL can contain a candidate booking id, but only the explicit response
    # field is authoritative here. Detail lookup must bind URL-only candidates
    # to this account and target before resuming a checkout or reporting success.
    if booking_id:
        return SubmitResult(
            SubmitOutcome.SUCCESS,
            booking_id=booking_id,
            url=booking_url_text or None,
        )

    errors = body.get("errors") or []
    if not isinstance(errors, list):
        return SubmitResult(SubmitOutcome.ERROR, message="예약 오류 응답 형식이 올바르지 않습니다", url=booking_url_text or None)
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        extensions = first.get("extensions")
        extensions = extensions if isinstance(extensions, dict) else {}
        code = str(extensions.get("code") or "")
        message = str(first.get("message") or "")
        reason = str(extensions.get("reason") or "")
        outcome = classify_submit_error(code, message, reason)
        if booking_url_text:
            outcome = SubmitOutcome.UNKNOWN
        # Any partial response URL may refer to a completed side effect. A
        # not-open wrapper must not cause another POST before it is inspected.
        has_not_open = any(
            value in SUBMIT_NOT_OPEN_CODES
            for error in errors if isinstance(error, Mapping)
            for value in _submit_error_tokens(error)
        )
        if (outcome == SubmitOutcome.NOT_OPEN or has_not_open) and (
            booking_url or not _exclusive_not_open_errors(errors)
        ):
            outcome = SubmitOutcome.UNKNOWN
        return SubmitResult(
            outcome,
            code=code or message,
            message=reason or message,
            url=booking_url_text or None,
        )

    if status >= 400:
        return SubmitResult(SubmitOutcome.ERROR, message=f"HTTP {status}", url=booking_url_text or None)
    return SubmitResult(SubmitOutcome.UNKNOWN if booking_url_text else SubmitOutcome.ERROR,
                        message="예약번호가 비어 있습니다", url=booking_url_text or None)


class NaverArmUncertainError(RuntimeError):
    """Chrome may own a live mutation; a second submit is unsafe."""


class NaverBrowserSubmitter:
    """Execute GraphQL inside the already-authenticated Naver page."""

    def __init__(self, page, timeout_seconds: float = 3.0) -> None:
        self.page = page
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self.last_rtt: float | None = None
        self.safe_rtt_samples: list[float] = []
        self.last_safe_rtt_at: float | None = None
        self._safe_rtt_history: list[tuple[float, float]] = []
        self._reconciliation_page = None
        self.reconciliation_baseline: NaverBookingSnapshot | None = None
        self.last_armed_timing: dict[str, float] = {}
        self.last_armed_diagnostics: dict[str, Any] = {}
        self.last_armed_visibility = "unknown"
        self.last_foreground_restore = "unavailable"
        # ``False`` distinguishes a failed GraphQL request from a successful
        # response that explicitly says the current browser is logged out.
        self.last_account_fetch_ok = False

    async def _graphql(
        self, operation_name: str, query: str, variables: dict[str, Any]
    ) -> Any:
        started = time.monotonic()
        response: Any = None
        try:
            response = await asyncio.wait_for(
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
            return response
        finally:
            elapsed = time.monotonic() - started
            measured = elapsed
            if isinstance(response, dict):
                try:
                    browser_elapsed = float(response.get("elapsedMs")) / 1000
                except (TypeError, ValueError):
                    browser_elapsed = 0.0
                if 0.005 <= browser_elapsed <= 3.0:
                    measured = browser_elapsed
            self.last_rtt = measured
            body = response.get("body") if isinstance(response, dict) else None
            healthy_read = (
                isinstance(response, dict) and response.get("status") == 200
                and isinstance(body, dict) and isinstance(body.get("data"), dict)
                and not body.get("errors")
            )
            if operation_name != "submitBooking" and healthy_read and 0.005 <= measured <= 3.0:
                self.last_safe_rtt_at = time.monotonic()
                self.safe_rtt_samples.append(measured)
                del self.safe_rtt_samples[:-8]
                self._safe_rtt_history.append((self.last_safe_rtt_at, measured))
                del self._safe_rtt_history[:-8]

    def recent_safe_rtt_samples(self, max_age_seconds: float = 15.0) -> list[float]:
        cutoff = time.monotonic() - max(0.0, float(max_age_seconds))
        return [rtt for recorded, rtt in self._safe_rtt_history if recorded >= cutoff]

    def reset_reconciliation_baseline(self) -> None:
        self.reconciliation_baseline = None

    async def _restore_submission_foreground(self) -> None:
        self.last_foreground_restore = "unavailable"
        try:
            is_closed = getattr(self.page, "is_closed", None)
            if callable(is_closed) and is_closed():
                self.last_foreground_restore = "closed"
                return
            bring_to_front = getattr(self.page, "bring_to_front", None)
            if callable(bring_to_front):
                await asyncio.wait_for(bring_to_front(), timeout=0.5)
                self.last_foreground_restore = "restored"
        except Exception:
            self.last_foreground_restore = "failed"

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

    async def _get_reconciliation_page(self):
        if self._reconciliation_page is not None:
            return self._reconciliation_page
        context = getattr(self.page, "context", None)
        if context is None or not hasattr(context, "new_page"):
            return None
        self._reconciliation_page = await asyncio.wait_for(context.new_page(), timeout=3.0)
        try:
            await self._reconciliation_page.goto(
                UPCOMING_BOOKINGS_URL, wait_until="domcontentloaded", timeout=5000,
            )
        except Exception:
            await self.close_reconciliation_page()
            raise
        return self._reconciliation_page

    async def close_reconciliation_page(self) -> None:
        page, self._reconciliation_page = self._reconciliation_page, None
        if page is not None:
            try:
                await asyncio.wait_for(page.close(), timeout=2.0)
            except Exception:
                pass

    async def _read_upcoming_bookings(self, page_number: int = 1) -> NaverBookingSnapshot:
        try:
            lookup_page = await self._get_reconciliation_page()
            if lookup_page is None:
                return NaverBookingSnapshot("unavailable", error_kind="no_browser_context")
            response = await asyncio.wait_for(lookup_page.evaluate(
                BROWSER_UPCOMING_BOOKINGS_SCRIPT, {
                    "endpoint": UPCOMING_BOOKINGS_ENDPOINT,
                    "query": UPCOMING_BOOKINGS_QUERY,
                    "timeoutMs": 2000, "page": page_number, "limit": 20,
                },
            ), timeout=2.5)
            return _booking_snapshot(response)
        except (asyncio.TimeoutError, TimeoutError):
            return NaverBookingSnapshot("network_error", error_kind="timeout")
        except Exception:
            return NaverBookingSnapshot("network_error", error_kind="browser_read_failed")

    async def preflight_reconciliation(self, max_pages: int = 5) -> NaverBookingSnapshot:
        """Warm the read-only page and capture existing ids before any submit."""
        self.reconciliation_baseline = None
        rows: list[Mapping[str, Any]] = []
        try:
            for page_number in range(1, max(1, min(int(max_pages), 10)) + 1):
                snapshot = await self._read_upcoming_bookings(page_number)
                if snapshot.state != "ok":
                    return replace(snapshot, pages_checked=page_number)
                rows.extend(snapshot.rows)
                snapshot = replace(
                    snapshot, rows=tuple(rows), pages_checked=page_number,
                    booking_ids=frozenset(str(row.get("id")) for row in rows if row.get("id")),
                )
                if not snapshot.has_more:
                    break
            self.reconciliation_baseline = snapshot
            return snapshot
        finally:
            if self.reconciliation_baseline is None:
                await self.close_reconciliation_page()
            await self._restore_submission_foreground()

    async def reconcile_upcoming_booking(
        self,
        *,
        target_date: str,
        target_time: str,
        business_id: str = "",
        item_name: str = "",
        attempts: int = 40,
        window_seconds: float = 20.0,
        baseline_booking_ids: frozenset[str] | set[str] | None = None,
        stop_event=None,
    ) -> NaverBookingReconciliation:
        """Read the logged-in account's booking list without another mutation."""
        baseline_complete = baseline_booking_ids is not None
        if baseline_booking_ids is None and self.reconciliation_baseline is not None:
            baseline_booking_ids = self.reconciliation_baseline.booking_ids
            baseline_complete = self.reconciliation_baseline.complete
        reads = failures = 0
        last_error = ""
        evidence = NaverBookingReconciliation(False, baseline_checked=baseline_booking_ids is not None)
        attempt_limit = max(1, min(int(attempts or 1), 128))
        deadline = time.monotonic() + max(0.5, min(float(window_seconds or 0.5), 90.0))
        page_number = 1
        try:
            for attempt in range(attempt_limit):
                if stop_event is not None and stop_event.is_set():
                    return replace(evidence, found=False, state="stopped")
                snapshot = await self._read_upcoming_bookings(page_number)
                if stop_event is not None and stop_event.is_set():
                    return replace(evidence, found=False, state="stopped")
                page_number = page_number + 1 if snapshot.has_more and page_number < 10 else 1
                if snapshot.state == "ok":
                    reads += 1
                    evidence = match_upcoming_booking(
                        snapshot.rows, target_date=target_date, target_time=target_time,
                        business_id=business_id, item_name=item_name,
                        baseline_booking_ids=baseline_booking_ids,
                    )
                else:
                    failures += 1
                    last_error = snapshot.error_kind
                    evidence = NaverBookingReconciliation(
                        False, state=snapshot.state,
                        baseline_checked=baseline_booking_ids is not None,
                    )
                evidence = replace(
                    evidence, attempts=attempt + 1, successful_reads=reads,
                    failed_reads=failures, http_status=snapshot.http_status,
                    error_kind=last_error, baseline_checked=baseline_complete,
                )
                if evidence.found or evidence.state in {
                    "existing", "ambiguous_match", "auth_error", "schema_error", "unavailable",
                } or snapshot.http_status == 429:
                    return evidence
                remaining = deadline - time.monotonic()
                if attempt + 1 >= attempt_limit or remaining <= 0:
                    break
                await asyncio.sleep(min(0.50 if snapshot.state == "ok" else 1.0, remaining))
        finally:
            await self.close_reconciliation_page()
        return evidence

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

    async def _browser_clock_bridge(self) -> tuple[float, float, float]:
        """Map Python monotonic milliseconds to this page's performance clock.

        These are local CDP reads, not server requests. The opening deadline was
        already derived from Naver currentDateTime by NaverServerClock.
        """
        samples = []
        for _ in range(3):
            before = time.monotonic() * 1000
            stamp = await asyncio.wait_for(self.page.evaluate(
                "() => ({now: performance.now(), origin: performance.timeOrigin})"
            ), timeout=0.25)
            after = time.monotonic() * 1000
            if not isinstance(stamp, dict):
                raise RuntimeError("Chrome 시계 응답 오류")
            now, origin = float(stamp["now"]), float(stamp["origin"])
            if not all(math.isfinite(v) for v in (now, origin)):
                raise RuntimeError("Chrome 시계 값 오류")
            samples.append((after - before, now - (before + after) / 2, origin))
        if len({row[2] for row in samples}) != 1:
            raise RuntimeError("Chrome 페이지가 시계 전송 중 이동했습니다")
        rtt, offset, origin = min(samples)
        return offset, rtt, origin

    async def _arm_submit(
        self,
        payload: dict[str, Any],
        delay_seconds: float,
        *,
        open_at_epoch: float | None = None,
        lead_seconds: float = 0.0,
        retry_lead_seconds: float = 0.0,
        target_arrival_before_open_seconds: float = 0.0,
        open_at_monotonic: float | None = None,
        clock_precision_seconds: float = 0.0,
    ) -> str:
        arm_id = f"naver-{time.monotonic_ns()}"
        # Capture before any awaits: preparing the bridge must not push the
        # relative fallback or the server deadline later.
        started = time.monotonic()
        open_deadline = (
            float(open_at_monotonic) if open_at_monotonic is not None
            else started + max(0.0, delay_seconds) + max(0.0, lead_seconds)
        )
        due_deadline = open_deadline - max(0.0, lead_seconds)
        await self._restore_submission_foreground()
        offset_ms, bridge_rtt_ms, origin = await self._browser_clock_bridge()
        self.last_armed_timing = {}
        try:
            response = await asyncio.wait_for(
                self.page.evaluate(
                    BROWSER_ARMED_SUBMIT_SCRIPT,
                    {
                        "armId": arm_id,
                        "input": copy.deepcopy(payload),
                        "query": SUBMIT_BOOKING_MUTATION,
                        "delayMs": round(max(0.0, float(delay_seconds)) * 1000),
                        "openAtEpochMs": round(float(open_at_epoch or 0.0) * 1000),
                        "leadMs": round(max(0.0, float(lead_seconds)) * 1000),
                        "retryLeadMs": round(
                            max(0.0, float(retry_lead_seconds)) * 1000
                        ),
                        "targetArrivalBeforeOpenMs": round(
                            float(target_arrival_before_open_seconds) * 1000
                        ),
                        "clockOrigin": origin,
                        "serverOpenAtPerfMs": open_deadline * 1000 + offset_ms if open_at_epoch else 0,
                        "dueAtPerfMs": due_deadline * 1000 + offset_ms,
                        "clockBridgeRttMs": bridge_rtt_ms,
                        "clockUncertaintyMs": max(0.0, clock_precision_seconds) * 1000 + bridge_rtt_ms / 2,
                        "timeoutMs": round(self.timeout_seconds * 1000),
                        "maxAttempts": 3,
                        "retryDelayMs": 10,
                        "retryWindowMs": 500,
                        "notOpenCodes": sorted(SUBMIT_NOT_OPEN_CODES),
                        "notOpenWrapperCodes": sorted(NOT_OPEN_WRAPPER_CODES),
                    },
                ),
                timeout=1.5,
            )
        except Exception as exc:
            # A lost evaluate reply is not evidence that Chrome failed to arm.
            # Fall back only after positively cancelling this exact timer.
            if await self.cancel_armed_submit(arm_id):
                raise RuntimeError("브라우저 예약 타이머 취소 확인") from exc
            raise NaverArmUncertainError("브라우저 예약 타이머 상태 불명확") from exc
        if isinstance(response, dict) and response.get("error") == "clock-context-changed":
            raise RuntimeError("Chrome 페이지 변경으로 타이머 설치하지 않음")
        if not isinstance(response, dict) or response.get("id") != arm_id:
            raise NaverArmUncertainError("브라우저 내부 예약 타이머 응답 불명확")
        visibility_value = response.get("armedVisibility")
        self.last_armed_visibility = visibility_value if visibility_value in {"visible", "hidden", "prerender"} else "unknown"
        for key in (
            "armedAt",
            "dueAt",
            "serverOpenAt",
            "clockRttMs",
            "clockBridgeRttMs",
            "clockSampleCount",
            "clockUncertaintyMs",
            "clockSpreadMs",
            "estimatedOutboundMs",
            "targetArrivalBeforeOpenMs",
            "appliedLeadMs",
            "delayMs",
        ):
            value = response.get(key)
            if isinstance(value, (int, float)):
                self.last_armed_timing[key] = float(value)
        return arm_id

    async def arm_submit_at(self, payload: dict[str, Any], delay_seconds: float) -> str:
        """Arm one flow using the caller's relative fallback timer."""
        return await self._arm_submit(payload, delay_seconds)

    async def arm_submit_at_server_time(
        self,
        payload: dict[str, Any],
        delay_seconds: float,
        *,
        open_at_epoch: float,
        lead_seconds: float,
        retry_lead_seconds: float,
        target_arrival_before_open_seconds: float = 0.0,
        open_at_monotonic: float | None = None,
        clock_precision_seconds: float = 0.0,
    ) -> str:
        """Transfer the synchronized Naver deadline into Chrome's clock domain."""
        return await self._arm_submit(
            payload,
            delay_seconds,
            open_at_epoch=open_at_epoch,
            lead_seconds=lead_seconds,
            retry_lead_seconds=retry_lead_seconds,
            target_arrival_before_open_seconds=target_arrival_before_open_seconds,
            open_at_monotonic=open_at_monotonic,
            clock_precision_seconds=clock_precision_seconds,
        )

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
        for key in (
            "armedAt",
            "dueAt",
            "serverOpenAt",
            "clockRttMs",
            "clockBridgeRttMs",
            "clockSampleCount",
            "clockUncertaintyMs",
            "clockSpreadMs",
            "estimatedOutboundMs",
            "targetArrivalBeforeOpenMs",
            "appliedLeadMs",
            "startedAt",
            "lastStartedAt",
            "headersAt",
            "responseBodyAt",
            "completedAt",
            "attempts",
            "notOpenAttempts",
        ):
            value = state.get(key)
            if isinstance(value, (int, float)):
                timing[key] = float(value)
        self.last_armed_timing = timing
        started_at = timing.get("startedAt", 0.0)
        last_started_at = timing.get("lastStartedAt", started_at)
        headers_at = timing.get("headersAt", 0.0)
        body_at = timing.get("responseBodyAt", 0.0)
        response = state.get("response")
        self.last_armed_diagnostics = {
            "attempts": float(timing.get("attempts", 0.0) or 0.0),
            "notOpenAttempts": float(
                timing.get("notOpenAttempts", 0.0) or 0.0
            ),
            "httpStatus": float(
                response.get("status", 0)
                if isinstance(response, dict)
                else 0
            ),
            "ttfbMs": (
                float(headers_at - last_started_at)
                if headers_at >= last_started_at > 0
                else 0.0
            ),
            "responseMs": (
                float(body_at - last_started_at)
                if body_at >= last_started_at > 0
                else 0.0
            ),
        }
        attempt_timings = []
        for record in (state.get("attemptTimings") or [])[:3]:
            if not isinstance(record, dict):
                continue
            clean = {
                key: float(record[key])
                for key in ("attempt", "dispatchAt", "headersAt", "bodyAt", "completedAt", "fetchStart", "requestStart", "responseStart", "responseEnd")
                if isinstance(record.get(key), (int, float)) and math.isfinite(record[key])
            }
            attempt_timings.append(clean)
        self.last_armed_diagnostics["attemptTimings"] = attempt_timings
        for key in ("armedVisibility", "dispatchVisibility"):
            value = state.get(key)
            self.last_armed_diagnostics[key] = value if value in {"visible", "hidden", "prerender"} else "unknown"
        self.last_armed_diagnostics["foregroundRestore"] = self.last_foreground_restore
        if attempt_timings:
            latest = attempt_timings[-1]
            dispatch = latest.get("dispatchAt", 0.0)
            request_start = latest.get("requestStart", 0.0)
            if request_start >= dispatch > 0:
                self.last_armed_diagnostics["browserQueueMs"] = request_start - dispatch
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
            return (await asyncio.wait_for(self.page.evaluate(
                BROWSER_CANCEL_ARMED_SUBMIT_SCRIPT, {"armId": arm_id}
            ), timeout=0.5)) is True
        except Exception:
            return False
