import json
from dataclasses import asdict
from urllib.parse import urlencode

import pytest

from engines.naver_booking import (
    BOOKING_DETAILS_QUERY,
    extract_booking_id_from_resume_url,
    verify_booking_details,
)


def _response(**changes):
    details = {
        "bookingId": "123456", "businessId": "843881", "bizItemId": "6627331",
        "bookingStatusCode": "RC03", "nPayChargedStatusCode": "CT02",
        "isPostPayment": False, "isMask": 0, "userId": "local-test-account",
        "snapshotJson": {"startDateTime": "2026-09-13T03:50:00.000Z",
                         "name": "private-name", "phone": "private-phone"},
    }
    details.update(changes)
    return {"data": {"bookingDetails": details}}


def _verify(response, **changes):
    target = dict(booking_id="123456", business_id="843881", biz_item_id="6627331",
                  target_date="2026-09-13", target_time="12:50", account_id="local-test-account")
    target.update(changes)
    return verify_booking_details(response, **target)


def test_query_is_read_only_and_contains_exact_target_and_status_fields():
    assert BOOKING_DETAILS_QUERY.startswith("query bookingDetails($input: BookingParams)")
    assert "mutation" not in BOOKING_DETAILS_QUERY
    assert "nPayChargedStatusCode" in BOOKING_DETAILS_QUERY
    assert "snapshotJson" in BOOKING_DETAILS_QUERY
    assert "phone" not in BOOKING_DETAILS_QUERY


def test_confirmed_prepaid_booking_requires_paid_axis_and_retains_no_private_data():
    result = _verify(_response())
    assert result.matched and result.confirmed and result.paid
    assert result.state == "confirmed"
    assert result.booking_id == "123456"
    serialized = json.dumps(asdict(result))
    assert "private-name" not in serialized
    assert "private-phone" not in serialized
    assert "local-test-account" not in serialized


@pytest.mark.parametrize("charged", ["CT01", "CT03", None, "unexpected"])
def test_rc03_alone_is_not_paid(charged):
    result = _verify(_response(nPayChargedStatusCode=charged))
    assert result.matched and result.confirmed and not result.paid
    assert result.state == "payment_pending"


def test_postpayment_rc03_is_confirmed_without_claiming_payment():
    result = _verify(_response(isPostPayment=True, nPayChargedStatusCode=None))
    assert result.state == "confirmed"
    assert result.matched and result.confirmed and not result.paid


@pytest.mark.parametrize("charged", ["CT01", "CT02", "CT03", None])
def test_rc02_is_pending_even_if_payment_is_present(charged):
    result = _verify(_response(bookingStatusCode="RC02", nPayChargedStatusCode=charged))
    assert result.state == "pending" and result.matched and not result.confirmed
    assert result.paid is (charged == "CT02")


@pytest.mark.parametrize("status", ["RC04", "RC05", "RC06", "RC08", None, "unexpected"])
def test_cancelled_or_unsupported_booking_cannot_be_confirmed(status):
    result = _verify(_response(bookingStatusCode=status))
    assert not result.matched and not result.confirmed and not result.paid


@pytest.mark.parametrize("charged", ["CT04", "CT05"])
def test_refunded_or_cancelled_payment_is_not_success(charged):
    result = _verify(_response(nPayChargedStatusCode=charged))
    assert result.state == "payment_cancelled" and not result.matched


@pytest.mark.parametrize("changes,state", [
    ({"bookingId": "123457"}, "id_mismatch"),
    ({"businessId": "different"}, "target_mismatch"),
    ({"bizItemId": "different"}, "target_mismatch"),
    ({"userId": "different"}, "identity_mismatch"),
    ({"userId": None}, "identity_mismatch"),
    ({"isMask": 1}, "masked"),
    ({"isMask": "1"}, "masked"),
    ({"snapshotJson": {}}, "target_unverified"),
    ({"snapshotJson": "truncated{"}, "target_unverified"),
    ({"snapshotJson": {"startDateTime": "2026-09-14T03:50:00Z"}}, "target_mismatch"),
    ({"snapshotJson": {"startDateTime": "2026-09-13T03:51:00Z"}}, "target_mismatch"),
])
def test_exact_identity_and_slot_are_required(changes, state):
    result = _verify(_response(**changes))
    assert result.state == state and not result.matched


@pytest.mark.parametrize("stamp", ["2026-09-13T12:50:00+09:00", "2026-09-13T12:50:00",
                                  "2026-09-12T23:50:00-04:00", "2026-09-13T03:50:00Z"])
def test_snapshot_string_and_timezone_are_normalized_to_kst(stamp):
    assert _verify(_response(snapshotJson=json.dumps({"startDateTime": stamp}))).matched


@pytest.mark.parametrize("mask", [None, 0, "0", False])
def test_unmasked_data_without_known_current_account_cannot_prove_ownership(mask):
    result = _verify(_response(isMask=mask), account_id="")
    assert result.state == "identity_ambiguous" and not result.matched


@pytest.mark.parametrize("response,state", [
    (None, "malformed"), ([], "malformed"), ({}, "malformed"),
    ({"data": {"bookingDetails": None}}, "not_found"),
    ({"data": {"bookingDetails": []}}, "malformed"),
    ({"errors": [{"extensions": {"code": "UNAUTHENTICATED"}}]}, "auth_required"),
    ({"errors": [{"message": "private server text"}]}, "lookup_error"),
])
def test_unusable_read_responses_fail_closed(response, state):
    result = _verify(response)
    assert result.state == state and not result.matched


def test_data_with_graphql_errors_does_not_override_errors():
    response = _response()
    response["errors"] = [{"extensions": {"code": "FORBIDDEN"}}]
    assert not _verify(response).matched


def test_http_wrapper_accepts_success_but_never_overrides_http_error():
    assert _verify({"status": 200, "body": _response()}).confirmed
    assert _verify({"status": 401, "body": _response()}).state == "auth_required"
    assert _verify({"status": 503, "body": _response()}).state == "lookup_error"


@pytest.mark.parametrize("changes", [{"bookingStatusCode": []}, {"nPayChargedStatusCode": []},
                                      {"isMask": {}}, {"snapshotJson": {"startDateTime": "2026-09-13 "}}])
def test_malformed_status_and_date_fields_fail_closed(changes):
    assert not _verify(_response(**changes)).matched


def test_known_return_url_yields_candidate_but_order_id_alone_does_not():
    resume = "https://m.booking.naver.com/my/bookings/123456?npay=1"
    pay = "https://orders.pay.naver.com/order/opaque?" + urlencode({"rurl": resume, "orderId": "987654"})
    assert extract_booking_id_from_resume_url(pay) == "123456"
    assert extract_booking_id_from_resume_url(resume) == "123456"
    assert extract_booking_id_from_resume_url("https://orders.pay.naver.com/order?orderId=123456") == ""


@pytest.mark.parametrize("url", [
    "http://m.booking.naver.com/my/bookings/123456",
    "https://m.booking.naver.com.evil.test/my/bookings/123456",
    "https://m.booking.naver.com@evil.test/my/bookings/123456",
    "https://m.booking.naver.com:444/my/bookings/123456",
    "https://m.booking.naver.com/my/bookings/123456/other",
    "https://m.booking.naver.com/my/bookings/not-a-number",
    "https://m.booking.naver.com/my/bookings/123456#another",
    "https://m.booking.naver.com/my/bookings/123456\n",
    "https://m.booking.naver.com\\@evil.test/my/bookings/123456",
])
def test_unsafe_or_nonexact_booking_url_is_not_candidate(url):
    assert extract_booking_id_from_resume_url(url) == ""
    wrapped = "https://orders.pay.naver.com/order?" + urlencode({"rurl": url})
    assert extract_booking_id_from_resume_url(wrapped) == ""


def test_multiple_return_urls_and_untrusted_payment_host_are_rejected():
    detail = "https://m.booking.naver.com/my/bookings/123456"
    duplicate = urlencode([("rurl", detail), ("rurl", detail)])
    assert extract_booking_id_from_resume_url("https://orders.pay.naver.com/?" + duplicate) == ""
    assert extract_booking_id_from_resume_url("https://pay.naver.com.evil.test/?" + urlencode({"rurl": detail})) == ""
