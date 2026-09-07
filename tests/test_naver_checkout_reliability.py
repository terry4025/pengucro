"""Checkout integration checks using fake browser responses, never live Naver."""

import asyncio
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from engines.naver_api import NaverAccount, SubmitOutcome, SubmitResult
from engines.naver_booking import BOOKING_DETAILS_QUERY
from engines.naver_submit import NaverBookingReconciliation, NaverSubmitPreparation, PAYMENT_NPAY_PREPAID
from test_naver_engine import ReconcilingSubmitter, make_engine


BOOKING_ID = "123456"
DETAIL_URL = f"https://m.booking.naver.com/my/bookings/{BOOKING_ID}"
PAY_URL = "https://orders.pay.naver.com/orderSheet/current-attempt"


@pytest.fixture(autouse=True)
def isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))


def details_response(**changes):
    details = {
        "bookingId": BOOKING_ID, "businessId": "843881", "bizItemId": "6627331",
        "bookingStatusCode": "RC03", "nPayChargedStatusCode": "CT02",
        "isPostPayment": False, "isMask": 0, "userId": "checkout-test-account",
        "snapshotJson": {"startDateTime": "2026-09-13T03:50:00.000Z"},
    }
    details.update(changes)
    return {"status": 200, "elapsedMs": 12, "body": {"data": {"bookingDetails": details}}}


class FakeBody:
    def __init__(self, text):
        self.text = text

    async def inner_text(self, **kwargs):
        return self.text


class FakePage:
    def __init__(self, url, response=None, on_query=None, text=""):
        self.url = url
        self.response = response
        self.on_query = on_query
        self.text = text
        self.requests = []
        self.navigations = []
        self.closed = 0
        self.foreground = 0

    async def evaluate(self, script, request):
        assert "fetch" in script
        self.requests.append(request)
        if self.on_query is not None:
            self.on_query()
        return self.response

    async def goto(self, url, **kwargs):
        self.navigations.append(url)
        self.url = url

    async def close(self):
        self.closed += 1

    async def bring_to_front(self):
        self.foreground += 1

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    def locator(self, selector):
        assert selector == "body"
        return FakeBody(self.text)


class FakeContext:
    def __init__(self, pages, new_page=None):
        self.pages = list(pages)
        self.next_page = new_page
        self.created = 0
        self.closed = 0

    async def new_page(self):
        assert self.next_page is not None
        self.created += 1
        self.pages.append(self.next_page)
        return self.next_page

    async def close(self):
        self.closed += 1


def checkout_engine():
    engine = make_engine()
    engine._api_account = NaverAccount(True, "unused-test-csrf", True, "checkout-test-account")
    engine._api_preparation = NaverSubmitPreparation(
        True, payload={"businessId": "843881", "bizItemId": "6627331"},
        payment_mode=PAYMENT_NPAY_PREPAID,
    )
    engine._reservation_target = {"reservationDate": "2026-09-13", "reservationTime": "12:50"}
    engine._npay_booking_id = BOOKING_ID
    engine.NPAY_MONITOR_INTERVAL_SECONDS = .001
    engine.NPAY_PAGE_TIMEOUT_SECONDS = .001
    return engine


def test_detail_read_uses_official_query_and_exact_current_target():
    engine = checkout_engine()
    page = FakePage(DETAIL_URL, details_response())
    engine._page = page
    result = asyncio.run(engine._read_booking_evidence(BOOKING_ID, page=page))
    assert result.matched and result.confirmed and result.paid
    assert len(page.requests) == 1
    request = page.requests[0]
    assert request["operationName"] == "bookingDetails"
    assert request["query"] == BOOKING_DETAILS_QUERY
    assert request["variables"] == {"input": {"bookingId": BOOKING_ID, "lang": "ko"}}
    assert page.navigations == []


@pytest.mark.parametrize("changes", [
    {"userId": "other-account"}, {"businessId": "other-business"},
    {"bizItemId": "other-item"}, {"isMask": 1},
    {"snapshotJson": {"startDateTime": "2026-09-13T03:51:00Z"}},
])
def test_detail_read_rejects_other_account_or_target(changes):
    engine = checkout_engine()
    page = FakePage(DETAIL_URL, details_response(**changes))
    result = asyncio.run(engine._read_booking_evidence(BOOKING_ID, page=page))
    assert result is not None and not result.matched
    assert len(page.requests) == 1


def test_detail_read_from_npay_uses_temporary_booking_page_and_restores_checkout():
    engine = checkout_engine()
    checkout = FakePage(PAY_URL)
    lookup = FakePage("about:blank", details_response())
    context = FakeContext([checkout], new_page=lookup)
    engine._page, engine._context = checkout, context
    result = asyncio.run(engine._read_booking_evidence(BOOKING_ID))
    assert result.matched and result.paid
    assert context.created == 1
    assert lookup.navigations == [DETAIL_URL]
    assert lookup.closed == 1
    assert checkout.closed == 0 and checkout.navigations == []
    assert checkout.foreground == 1
    assert engine._page is checkout


@pytest.mark.parametrize("status,charge", [("RC02", "CT01"), ("RC02", "CT02"), ("RC03", "CT01")])
def test_detail_url_and_completion_text_do_not_confirm_unpaid_or_pending_booking(status, charge):
    engine = checkout_engine()
    page = FakePage(DETAIL_URL, details_response(bookingStatusCode=status, nPayChargedStatusCode=charge),
                    on_query=engine.stop_event.set, text="예약 완료 안내 및 결제 완료 후 이용 안내")
    engine._page = page
    result = asyncio.run(engine._monitor_npay_completion())
    assert result == ""
    assert len(page.requests) == 1


def test_monitor_confirms_only_exact_booking_rc03_and_ct02():
    engine = checkout_engine()
    page = FakePage(DETAIL_URL, details_response())
    engine._page = page
    result = asyncio.run(asyncio.wait_for(engine._monitor_npay_completion(), timeout=.5))
    assert BOOKING_ID in result and "결제 완료 확인" in result
    assert len(page.requests) == 1


def test_monitor_finds_paid_booking_even_when_checkout_redirect_is_lost():
    engine = checkout_engine()
    checkout = FakePage(PAY_URL, text="결제 요청을 처리하고 있습니다")
    lookup = FakePage("about:blank", details_response())
    engine._page, engine._context = checkout, FakeContext([checkout], new_page=lookup)
    result = asyncio.run(asyncio.wait_for(engine._monitor_npay_completion(), timeout=.5))
    assert BOOKING_ID in result and "결제 완료 확인" in result
    assert checkout.navigations == []
    assert lookup.closed == 1 and len(lookup.requests) == 1


def test_monitor_rejects_a_paid_booking_owned_by_another_account():
    engine = checkout_engine()
    page = FakePage(DETAIL_URL, details_response(userId="other-account"), on_query=engine.stop_event.set)
    engine._page = page
    assert asyncio.run(engine._monitor_npay_completion()) == ""
    assert len(page.requests) == 1


def test_detail_read_stops_if_current_account_cannot_be_identified():
    engine = checkout_engine()
    engine._api_account = None
    page = FakePage(DETAIL_URL, details_response(isMask=0))
    result = asyncio.run(engine._read_booking_evidence(BOOKING_ID, page=page))
    assert result is None
    assert [request["operationName"] for request in page.requests] == ["account"]


@pytest.mark.parametrize("source", ["timeline", "partial_url"])
def test_paid_rc02_recovery_does_not_enter_checkout_or_report_final_success(source):
    engine = checkout_engine()
    checkout = FakePage(PAY_URL)
    lookup = FakePage("about:blank", details_response(bookingStatusCode="RC02", nPayChargedStatusCode="CT02"))
    engine._page, engine._context = checkout, FakeContext([checkout], new_page=lookup)
    engine._api_submitter = ReconcilingSubmitter([], NaverBookingReconciliation(
        True, booking_id=BOOKING_ID, url=DETAIL_URL, status="RC02",
    ))

    async def no_inventory(_reservation_data):
        return []

    async def forbidden(*args, **kwargs):
        raise AssertionError("A paid RC02 reservation must wait for confirmation, never pay again")

    engine._observe_post_submit_inventory = no_inventory
    engine._continue_npay_checkout = forbidden
    if source == "timeline":
        result = asyncio.run(engine._reconcile_ambiguous_api_submit(
            reservation_data=engine._reservation_target, dev_mode=False,
        ))
    else:
        candidate = PAY_URL + "?" + urlencode({"rurl": DETAIL_URL})
        result = asyncio.run(engine._handle_api_submit_result(
            SubmitResult(SubmitOutcome.UNKNOWN, url=candidate), signature=None,
            reservation_data=engine._reservation_target, dev_mode=False,
        ))
    assert result[0] == "pending" and "결제 완료 확인" in result[1]
    assert engine._api_submitter.calls == 0
    assert len(lookup.requests) == 1 and lookup.closed == 1


def test_wait_for_checkout_ignores_existing_unrelated_npay_tab():
    engine = checkout_engine()
    current = FakePage("https://m.booking.naver.com/booking/12/bizes/843881/items/6627331")
    old = FakePage("https://orders.pay.naver.com/orderSheet/other-attempt")
    engine._page, engine._context = current, FakeContext([current, old])
    assert asyncio.run(engine._wait_for_npay_page("")) is None
    assert old.foreground == 0 and old.navigations == []


def test_wait_for_checkout_selects_exact_returned_url_not_newest_unrelated_tab():
    engine = checkout_engine()
    current = FakePage(DETAIL_URL)
    correct = FakePage(PAY_URL)
    wrong = FakePage("https://orders.pay.naver.com/orderSheet/another-attempt")
    engine._page, engine._context = current, FakeContext([current, correct, wrong])
    assert asyncio.run(engine._wait_for_npay_page(PAY_URL)) is correct
    assert correct.foreground == 1 and wrong.foreground == 0
    assert current.navigations == []


def test_checkout_without_booking_id_cannot_select_money_or_press_final_pay():
    engine = checkout_engine()
    engine._npay_booking_id = ""
    page = FakePage(PAY_URL)
    engine._page = page

    async def checkout(_url):
        return page

    async def forbidden(*args, **kwargs):
        raise AssertionError("An unidentified checkout must not select or submit payment")

    engine._navigate_to_npay_page = checkout
    engine._select_npay_money = forbidden
    engine._find_npay_pay_button = forbidden
    result, detail = asyncio.run(engine._continue_npay_checkout(
        booking_id="", payment_url=PAY_URL, dev_mode=False, navigate_immediately=True,
    ))
    assert result == "unknown" and "자동 결제 없이" in detail
    assert engine._preserve_checkout_page and not page.closed


def test_stale_tab_cleanup_preserves_other_active_booking_and_payment_tabs():
    engine = checkout_engine()
    pages = [FakePage(DETAIL_URL + f"?attempt={index}") for index in range(engine.STALE_TAB_LIMIT + 2)]
    pages.append(FakePage(PAY_URL))
    engine._context = FakeContext(pages)
    asyncio.run(engine._close_stale_tabs())
    assert all(page.closed == 0 for page in pages)


@pytest.mark.parametrize("owns_browser", [False, True])
def test_teardown_preserves_checkout_and_actual_chrome(owns_browser):
    engine = checkout_engine()
    page = FakePage(PAY_URL)
    context = FakeContext([page])
    browser = FakeContext([])
    stopped = []
    chrome_closed = []

    async def stop_playwright():
        stopped.append(True)

    engine._page, engine._context, engine.browser = page, context, browser
    engine._owns_browser = owns_browser
    engine._preserve_checkout_page = True
    engine._close_chrome_on_exit = True
    engine._playwright = SimpleNamespace(stop=stop_playwright)
    engine._chrome_session = SimpleNamespace(close_if_launched=lambda: chrome_closed.append(True))
    asyncio.run(engine._teardown_browser())
    assert page.closed == 0 and context.closed == 0 and chrome_closed == []
    if owns_browser:
        assert browser.closed == 0 and stopped == []
    else:
        # Closing the CDP connection detaches Playwright, not Chrome itself.
        assert browser.closed == 1 and stopped == [True]
