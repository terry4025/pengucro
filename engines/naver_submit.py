"""Build and send Naver's direct ``submitBooking`` request.

The page itself still owns authentication. This module only supports the simple
EPISODE shape used by the escape-room products: no seats, no fixed period and no
Naver Pay checkout. Unsupported products deliberately fall back to the browser
flow instead of guessing at a payment or seating payload.
"""

from __future__ import annotations

import copy
import re
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


EPISODE_BUSINESS_TYPE_ID = 12
TERMS_VERSION = "20251030"


@dataclass(frozen=True)
class NaverSubmitPreparation:
    ready: bool
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    slot_id: str = ""


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


def _first_resource(resources: Any) -> str | None:
    if not isinstance(resources, list) or not resources:
        return None
    first = resources[0] if isinstance(resources[0], dict) else {}
    return str(first.get("resourceUrl") or "") or None


def _custom_form_input(
    form: Any, people: Any
) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(form, list):
        return [], None
    wanted = str(people or "").strip()
    wanted_digits = _digits(wanted)
    output: list[dict[str, Any]] = []
    for raw_question in form:
        if not isinstance(raw_question, dict):
            continue
        question = copy.deepcopy(raw_question)
        required = str(question.get("required") or "").lower() == "y"
        question_type = str(question.get("type") or "").upper()
        if question_type != "SELECT":
            if required:
                return [], "지원하지 않는 필수 추가 입력 항목이 있습니다"
            output.append(question)
            continue

        selected = None
        for option in question.get("options") or []:
            if not isinstance(option, dict):
                continue
            value = str(option.get("value") or "")
            if value == wanted or (
                wanted_digits and _digits(value) == wanted_digits
            ):
                selected = option
                break
        if selected is None:
            if required:
                return [], "예약 인원에 맞는 필수 추가 입력 선택지가 없습니다"
            output.append(question)
            continue
        question["value"] = selected.get("value")
        question["originalValue"] = (
            selected.get("originalValue") or selected.get("value")
        )
        output.append(question)
    return output, None


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
        if int(business.get("businessTypeId") or 0) != EPISODE_BUSINESS_TYPE_ID:
            return NaverSubmitPreparation(
                False, reason="이 상품 유형의 API 직접 제출은 지원하지 않습니다"
            )
        if (
            biz_item.get("isSeatUsed")
            or biz_item.get("isPeriodFixed")
            or biz_item.get("isNPayUsed")
        ):
            return NaverSubmitPreparation(
                False,
                reason="좌석제·기간제·네이버페이 상품은 API 직접 제출을 지원하지 않습니다",
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

        custom_form, form_error = _custom_form_input(
            business.get("customFormJson"), reservation.get("people")
        )
        if form_error:
            return NaverSubmitPreparation(False, reason=form_error)

        prices = [
            copy.deepcopy(price)
            for price in (slot.get("prices") or [])
            if isinstance(price, dict)
            and price.get("isImp", True) is not False
        ]
        if len(prices) != 1:
            return NaverSubmitPreparation(
                False, reason="가격 선택지가 한 개가 아닌 상품은 지원하지 않습니다"
            )
        booking_count = max(1, int(slot.get("minBookingCount") or 1))
        max_count = int(slot.get("maxBookingCount") or booking_count)
        if booking_count > max_count:
            return NaverSubmitPreparation(
                False, reason="슬롯의 최소·최대 예약 수량이 올바르지 않습니다"
            )
        prices[0]["bookingCount"] = booking_count
        base_price = int(prices[0].get("price") or 0) * booking_count
        if base_price < 0:
            return NaverSubmitPreparation(
                False, reason="슬롯 가격이 올바르지 않습니다"
            )

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
            "businessTypeId": EPISODE_BUSINESS_TYPE_ID,
            "isNPayUsed": False,
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
            "isPostPayment": False,
            "slotId": str(slot.get("slotId") or ""),
            "startMinute": start_minute,
            "endDate": start_kst.strftime("%Y-%m-%d"),
            "bizItemPrice": base_price,
            "slotName": str(slot.get("name") or ""),
            "visitorName": "",
            "visitorPhone": "",
            "hasVisitor": False,
            "paymentSettingJson": None,
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
        )


BROWSER_GRAPHQL_SCRIPT = r"""async request => {
    const variables = request.variables || {};
    if (request.operationName === "submitBooking" &&
            variables.input && variables.input.userAgentJson) {
        const ua = navigator.userAgent || "";
        const platform = (navigator.userAgentData &&
            navigator.userAgentData.platform) || navigator.platform || "";
        variables.input.userAgentJson = {
            ...variables.input.userAgentJson,
            raw: ua,
            os: platform,
            device: /Mobi|Android/i.test(ua) ? "MOBILE" : "PC",
        };
    }
    const response = await fetch("/graphql", {
        method: "POST",
        credentials: "include",
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

    errors = body.get("errors") or []
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        extensions = first.get("extensions")
        extensions = extensions if isinstance(extensions, dict) else {}
        code = str(extensions.get("code") or "")
        message = str(first.get("message") or "")
        reason = str(extensions.get("reason") or "")
        return SubmitResult(
            classify_submit_error(code, message),
            code=code or message,
            message=reason or message,
        )

    booking = ((body.get("data") or {}).get("submitBooking")) or {}
    booking_id = str(booking.get("bookingId") or "")
    if booking_id:
        return SubmitResult(
            SubmitOutcome.SUCCESS,
            booking_id=booking_id,
            url=booking.get("url"),
        )
    if status >= 400:
        return SubmitResult(SubmitOutcome.ERROR, message=f"HTTP {status}")
    return SubmitResult(SubmitOutcome.ERROR, message="예약번호가 비어 있습니다")


class NaverBrowserSubmitter:
    """Execute GraphQL inside the already-authenticated Naver page."""

    def __init__(self, page) -> None:
        self.page = page

    async def _graphql(
        self, operation_name: str, query: str, variables: dict[str, Any]
    ) -> Any:
        return await self.page.evaluate(
            BROWSER_GRAPHQL_SCRIPT,
            {
                "operationName": operation_name,
                "query": query,
                "variables": variables,
            },
        )

    async def fetch_account(self) -> NaverAccount:
        try:
            response = await self._graphql("account", ACCOUNT_QUERY, {})
        except Exception:
            return NaverAccount(False, "", False)
        body = response.get("body") if isinstance(response, dict) else None
        data = body.get("data") if isinstance(body, dict) else None
        account = data.get("account") if isinstance(data, dict) else None
        if not isinstance(account, dict):
            return NaverAccount(False, "", False)
        return NaverAccount(
            is_logged_in=bool(account.get("isLoggedIn")),
            csrf_token=str(account.get("csrfToken") or ""),
            is_sms_alarm=bool(account.get("isSmsAlarm")),
            user_id=str(account.get("userId") or ""),
            nickname=str(account.get("nickname") or ""),
        )

    async def submit(self, payload: dict[str, Any]) -> SubmitResult:
        try:
            response = await self._graphql(
                "submitBooking",
                SUBMIT_BOOKING_MUTATION,
                {"input": copy.deepcopy(payload)},
            )
        except Exception as exc:
            return SubmitResult(
                SubmitOutcome.ERROR,
                message=f"브라우저 GraphQL 전송 실패 ({type(exc).__name__})",
            )
        result = _submit_result_from_response(response)
        return SubmitResult(
            result.outcome,
            code=_redact_payload_values(result.code, payload),
            message=_redact_payload_values(result.message, payload),
            booking_id=result.booking_id,
            url=result.url,
        )
