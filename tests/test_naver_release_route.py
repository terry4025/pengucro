"""Real app dispatch and worker, with browser/network boundaries replaced."""

import json
import queue
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from engines.naver_api import NaverAccount
from engines.naver_engine import NaverEngine
from engines.naver_shared import NaverSharedCoordinator
from engines.naver_submit import NaverBrowserSubmitter, NaverSubmitPreparation, PAYMENT_NPAY_PREPAID
from pengucro.models import NAVER_MODE, ReservationRequest
from ui.main_window import MainWindow
from test_naver_engine import FakeClock, make_slot


BOOKING_ID = "999888"
ITEM_URL = "https://m.booking.naver.com/booking/12/bizes/1498729/items/6282267"
DETAIL_URL = f"https://m.booking.naver.com/my/bookings/{BOOKING_ID}"
PAY_URL = "https://orders.pay.naver.com/orderSheet/test-current-attempt"
ACCOUNT = "release-route-test-account"
TARGET = dict(reservationDate="2030-09-13", reservationTime="12:50", themePK=ITEM_URL,
              themeLabel="바야흐로,여름이었다.", name="테스트", phone="01000000001", people=2)


class BrowserBoundary:
    def __init__(self, scenario):
        self.scenario = scenario
        self.posts = []
        self.reads = []
        self.states_at_post = []
        self.pages = []
        self.engine = None
        self.main = PageBoundary(self, main=True)
        self.pages.append(self.main)

    async def new_page(self):
        page = PageBoundary(self)
        self.pages.append(page)
        return page


class PageBoundary:
    def __init__(self, context, *, main=False):
        self.context = context
        self.main = main
        self.url = ITEM_URL if main else "about:blank"
        self.closed = False

    async def goto(self, url, **_kwargs):
        self.url = PAY_URL if self.main and url == DETAIL_URL else url

    async def close(self):
        self.closed = True

    async def bring_to_front(self):
        pass

    def is_closed(self):
        return self.closed

    async def evaluate(self, _script, request):
        context = self.context
        operation = request.get("operationName", "upcomingBookings")
        if operation == "submitBooking":
            context.posts.append(request)
            context.states_at_post.append(context.engine._api_submit_state)
            if context.scenario != "direct_dev":
                raise TimeoutError("response lost after dispatch")
            return {"status": 200, "body": {"data": {"submitBooking": {
                "bookingId": BOOKING_ID, "url": PAY_URL,
            }}}}
        context.reads.append(operation)
        if operation == "upcomingBookings":
            rows = []
            if context.posts and context.scenario != "lost_unknown":
                rows.append(dict(id=BOOKING_ID, businessId="1498729",
                                 bizItemName=TARGET["themeLabel"],
                                 formattedBookingDateText="2030. 9. 13. 오후 12:50",
                                 bookingStatusCode="RC03", landingUrl=DETAIL_URL))
            return {"status": 200, "body": {"data": {"me": {
                "__typename": "MeSucceed", "upcomingBookings": {
                    "bookings": rows, "pageInfo": {"hasNextPage": False},
                },
            }}}}
        assert operation == "bookingDetails"
        assert request["variables"]["input"]["bookingId"] == BOOKING_ID
        return {"status": 200, "body": {"data": {"bookingDetails": dict(
            bookingId=BOOKING_ID, businessId="1498729", bizItemId="6282267",
            bookingStatusCode="RC03",
            nPayChargedStatusCode="CT02" if context.scenario == "lost_paid" else "CT01",
            isPostPayment=False, isMask=0, userId=ACCOUNT,
            snapshotJson={"startDateTime": "2030-09-13T03:50:00.000Z"},
        )}}}


def app_boundary(dev_mode):
    form = Mock()
    form.developer_mode_enabled.return_value = dev_mode
    form.engine_mode_btn.get.return_value = NAVER_MODE
    return SimpleNamespace(
        _catalog_refresh_running=False, _keyescape_cache_running=False,
        site_var=SimpleNamespace(get=lambda: "드림이스케이프 건대"), form=form,
        log_panel=Mock(), site_dropdown=Mock(), add_site_btn=Mock(), delete_site_btn=Mock(),
        cta_btn=Mock(), _set_status_badge=Mock(), engine_event_queue=queue.Queue(),
        custom_sites={"드림이스케이프 건대": {"style": "naver"}},
        _on_engine_log=Mock(), _on_booking_success=Mock(), _on_engine_status_update=Mock(),
        _on_engine_log_batch=Mock(), _update_booking_status=Mock(), _reset_cta_state=Mock(),
    )


@pytest.mark.parametrize("scenario", ["direct_dev", "lost_recovered_dev", "lost_unknown", "lost_paid"])
def test_mainwindow_registry_base_worker_naver_submission_contract(monkeypatch, tmp_path, scenario):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    boundary = BrowserBoundary(scenario)
    dev_stopped_before_pay = Event()
    captured = {}
    final_pay = Mock(side_effect=AssertionError("This route must never click final payment"))

    async def prepare_external_services(engine, workers, data):
        # Replace only startup I/O. MainWindow, registry, BaseEngine worker,
        # actual submit parser, result handling, lookup and disk guard all run.
        captured.update(workers=workers, payload=dict(data), initial_state=engine._api_submit_state)
        boundary.engine = engine
        engine.session_pool = []
        engine.api = SimpleNamespace(
            find_slot=lambda *_args, **_kwargs: make_slot(unitStartTime="2030-09-13 12:50:00"),
            close=Mock(), last_rtt=None,
        )
        engine.clock = FakeClock(-1)
        engine._reservation_target = data
        engine._page, engine._context = boundary.main, boundary
        engine._api_account = NaverAccount(True, "test-csrf", False, ACCOUNT)
        engine._api_biz_item = {"name": TARGET["themeLabel"]}
        engine._shared_reads = NaverSharedCoordinator(tmp_path / "shared", read_interval=0)
        engine._api_preparation = NaverSubmitPreparation(
            True, payload={"businessId": "1498729", "bizItemId": "6282267", "slotId": "1"},
            slot_id="1", payment_mode=PAYMENT_NPAY_PREPAID,
        )
        engine._api_submitter = NaverBrowserSubmitter(boundary.main)
        baseline = await engine._api_submitter.preflight_reconciliation()
        assert baseline.complete and not baseline.booking_ids
        engine._api_submit_enabled = True
        engine.API_RECONCILE_ATTEMPTS = 1
        engine.API_POST_SUBMIT_INVENTORY_OFFSETS = (0,)

    async def select_money(_engine, page):
        assert page is boundary.main and page.url == PAY_URL
        return True, "test money control"

    async def payment_control(_engine, page):
        assert page is boundary.main
        return SimpleNamespace(click=final_pay), "결제하기"

    async def capture_dev_screen(_engine, page, _path):
        assert page.url == PAY_URL
        dev_stopped_before_pay.set()

    monkeypatch.setattr(NaverEngine, "pre_fetch_sessions_async", prepare_external_services)
    monkeypatch.setattr(NaverEngine, "_select_npay_money", select_money)
    monkeypatch.setattr(NaverEngine, "_find_npay_pay_button", payment_control)
    monkeypatch.setattr(NaverEngine, "_dump_debug", capture_dev_screen)
    dev_mode = scenario.endswith("dev")
    app = app_boundary(dev_mode)
    # An intentionally stale request flag must be overwritten by the visible UI.
    request = ReservationRequest.from_mapping("드림이스케이프 건대", {**TARGET, "devMode": not dev_mode})
    MainWindow._start_booking(app, request, 50, False)
    engine = app.active_engine
    assert isinstance(engine, NaverEngine)
    try:
        assert engine.async_thread is not None
        if dev_mode:
            assert dev_stopped_before_pay.wait(3), app._on_engine_log.call_args_list
            assert engine.is_running
            assert engine._npay_booking_id == BOOKING_ID
            engine.stop_reservation()
        engine.async_thread.join(timeout=4)
        assert not engine.async_thread.is_alive(), app._on_engine_log.call_args_list
        assert not engine.is_running
        assert captured["workers"] == 1 and engine.threads == []
        assert captured["initial_state"] == "idle"
        assert captured["payload"]["reservationDate"] == TARGET["reservationDate"]
        assert captured["payload"]["reservationTime"] == "12:50:00"
        assert captured["payload"]["themePK"] == ITEM_URL
        assert captured["payload"]["people"] == "2"
        assert captured["payload"]["devMode"] is dev_mode
        assert len(boundary.posts) == 1 and boundary.states_at_post == ["inflight"]
        final_pay.assert_not_called()
        expected_state = "uncertain" if scenario == "lost_unknown" else "success"
        assert engine._api_submit_state == expected_state
        persisted = json.loads(engine._shared_reads.submission_path.read_text(encoding="utf-8"))
        assert len(persisted) == 1
        assert next(iter(persisted.values()))["state"] == (
            "uncertain" if scenario == "lost_unknown" else "confirmed"
        )
        assert engine._success_fired is (scenario == "lost_paid")
        assert app._on_booking_success.call_count == int(scenario == "lost_paid")
        if scenario != "direct_dev":
            assert boundary.reads.count("upcomingBookings") == 2
        if scenario in {"lost_recovered_dev", "lost_paid"}:
            assert boundary.reads.count("bookingDetails") == 1
            assert any(BOOKING_ID in str(call) for call in app._on_engine_log.call_args_list)
        assert all(page.closed for page in boundary.pages if not page.main)
        assert boundary.main.closed is (not dev_mode)
        engine.api.close.assert_called_once()
    finally:
        engine.stop_reservation()
        if engine.async_thread is not None:
            engine.async_thread.join(timeout=2)
