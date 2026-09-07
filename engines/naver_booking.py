"""Validate read-only official booking details without retaining private data.

Public sources inspected 2026-09-07:
https://m.booking.naver.com/mobile/static/js/main.696e1655.js
https://m.booking.naver.com/mobile/static/js/Booked-Check-Bridge.e6541d8e.chunk.js
The first declares RC02=requested, RC03=confirmed and CT02=paid. The second
uses bookingDetails with bookingId/lang. These are frontend schema/state
evidence, not a live verification of any reservation. isMask=0 only indicates
unmasked data, so known current-account identity is still required below.
No Npay order-to-booking lookup is assumed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit


KST = timezone(timedelta(hours=9))
BOOKING_DETAILS_QUERY = """query bookingDetails($input: BookingParams) {
  bookingDetails(input: $input) {
    bookingId
    businessId
    bizItemId
    bookingStatusCode
    nPayChargedStatusCode
    isPostPayment
    useNaverPay
    isMask
    userId
    snapshotJson
  }
}"""


@dataclass(frozen=True)
class VerifiedBookingDetails:
    state: str
    matched: bool = False
    confirmed: bool = False
    paid: bool = False
    booking_id: str = ""


def _mask_flag(value: Any) -> bool | None:
    if value is True or value == 1 or value == "1":
        return True
    if value is False or value == 0 or value == "0":
        return False
    return None


def _snapshot_datetime(snapshot: Any) -> datetime | None:
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (ValueError, TypeError):
            return None
    if not isinstance(snapshot, dict):
        return None
    raw = snapshot.get("startDateTime")
    if not isinstance(raw, str) or not re.match(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d", raw.strip()):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KST)
        return parsed.astimezone(KST)
    except (ValueError, OverflowError):
        return None


def verify_booking_details(
    response: Any, *, booking_id: str, business_id: str = "", biz_item_id: str = "",
    target_date: str = "", target_time: str = "", account_id: str = "",
) -> VerifiedBookingDetails:
    """Check identity/target first, then report reservation and payment separately.

    ``confirmed`` means RC03, while ``paid`` means CT02. A prepaid booking
    requires BOTH before reporting final completion. An RC02 match remains
    pending even when paid. Private response fields never enter the result.
    """
    expected_id = str(booking_id or "")
    if not re.fullmatch(r"[0-9]+", expected_id):
        return VerifiedBookingDetails("invalid_target")
    if not isinstance(response, dict):
        return VerifiedBookingDetails("malformed")
    if "body" in response:
        status = response.get("status")
        if status in (401, 403):
            return VerifiedBookingDetails("auth_required")
        if not isinstance(status, int) or not 200 <= status < 300:
            return VerifiedBookingDetails("lookup_error")
        response = response["body"]
        if not isinstance(response, dict):
            return VerifiedBookingDetails("malformed")
    errors = response.get("errors")
    if errors:
        codes = {str(error.get("extensions", {}).get("code", "")).upper()
                 for error in errors if isinstance(error, dict)
                 and isinstance(error.get("extensions", {}), dict)} if isinstance(errors, list) else set()
        state = "auth_required" if codes & {"UNAUTHENTICATED", "UNAUTHORIZED", "FORBIDDEN"} else "lookup_error"
        return VerifiedBookingDetails(state)
    data = response.get("data")
    if not isinstance(data, dict):
        return VerifiedBookingDetails("malformed")
    details = data.get("bookingDetails")
    if details is None:
        return VerifiedBookingDetails("not_found")
    if not isinstance(details, dict):
        return VerifiedBookingDetails("malformed")
    if str(details.get("bookingId") or "") != expected_id:
        return VerifiedBookingDetails("id_mismatch")
    masked = _mask_flag(details.get("isMask"))
    if masked is True:
        return VerifiedBookingDetails("masked")
    if masked is None and details.get("isMask") is not None:
        return VerifiedBookingDetails("malformed")
    if account_id:
        if str(details.get("userId") or "") != str(account_id):
            return VerifiedBookingDetails("identity_mismatch")
    else:
        return VerifiedBookingDetails("identity_ambiguous")
    for expected, field in ((business_id, "businessId"), (biz_item_id, "bizItemId")):
        if expected and str(details.get(field) or "") != str(expected):
            return VerifiedBookingDetails("target_mismatch")
    if target_date or target_time:
        actual_time = _snapshot_datetime(details.get("snapshotJson"))
        if actual_time is None:
            return VerifiedBookingDetails("target_unverified")
        try:
            if target_date and actual_time.date() != date.fromisoformat(str(target_date)):
                return VerifiedBookingDetails("target_mismatch")
            if target_time:
                expected_time = time.fromisoformat(str(target_time))
                if expected_time.tzinfo is not None:
                    return VerifiedBookingDetails("invalid_target")
                if actual_time.time() != expected_time:
                    return VerifiedBookingDetails("target_mismatch")
        except ValueError:
            return VerifiedBookingDetails("invalid_target")
    status = details.get("bookingStatusCode")
    if not isinstance(status, str):
        return VerifiedBookingDetails("invalid_status")
    if status in {"RC04", "RC05", "RC06"}:
        return VerifiedBookingDetails("cancelled")
    if status not in {"RC02", "RC03"}:
        return VerifiedBookingDetails("invalid_status")
    charged = details.get("nPayChargedStatusCode")
    if charged is not None and not isinstance(charged, str):
        return VerifiedBookingDetails("malformed")
    if charged in {"CT04", "CT05"}:
        return VerifiedBookingDetails("payment_cancelled")
    confirmed = status == "RC03"
    paid = charged == "CT02"
    state = "confirmed" if confirmed else "pending"
    if confirmed and details.get("isPostPayment") is not True and not paid:
        state = "payment_pending"
    return VerifiedBookingDetails(state, True, confirmed, paid, expected_id)


def _secure_url(value: Any):
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment):
            return None
        return parsed
    except ValueError:
        return None


def _details_id(parsed) -> str:
    if parsed is None or parsed.hostname != "m.booking.naver.com":
        return ""
    match = re.fullmatch(r"/my/bookings/([0-9]+)", parsed.path)
    return match.group(1) if match else ""


def extract_booking_id_from_resume_url(value: Any) -> str:
    """Extract only a candidate ID, never infer it from an Npay orderId.

    Accept the official details URL or its single ``rurl`` inside an HTTPS
    Npay URL. Call verify_booking_details before trusting the candidate.
    """
    parsed = _secure_url(value)
    direct = _details_id(parsed)
    if direct:
        return direct
    if parsed is None:
        return ""
    host = parsed.hostname or ""
    if host != "pay.naver.com" and not host.endswith(".pay.naver.com"):
        return ""
    try:
        returns = parse_qs(parsed.query, max_num_fields=64).get("rurl", [])
    except ValueError:
        return ""
    if len(returns) != 1:
        return ""
    return _details_id(_secure_url(returns[0]))
