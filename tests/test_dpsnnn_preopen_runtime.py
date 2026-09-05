from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest
import requests

import engines.dpsnnn_engine as module
from engines.dpsnnn_engine import DpsnnnEngine, parse_dpsnnn_calendar
from engines.dpsnnn_orders import OrderJournal
from engines.dpsnnn_runtime import ReadGovernor, WarmCheckout, detail_slot
from engines.zeroworld_catalog import ZeroWorldTimeSlot as Slot
from pengucro.dpsnnn_plan import load_plan


def request_data(alias="행복", at="17:50", phone="01000000001"):
    return {"branch": "gangnam", "themePK": alias, "reservationDate": "2026-09-12",
            "reservationTime": at + ":00", "name": "테스트예약자", "phone": phone,
            "people": "2"}


def prepare(monkeypatch, fetch):
    engine = DpsnnnEngine(lambda *args: None)
    engine.POLL_INTERVAL = 0
    engine.WORKER_STAGGER = 0
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None)
    session = SimpleNamespace(get=lambda *a, **k: response, close=lambda: None, cookies=[])
    monkeypatch.setattr(module, "create_dpsnnn_session", lambda: session)
    monkeypatch.setattr(module, "fetch_exact_dpsnnn_slots", fetch)
    monkeypatch.setattr(engine, "_build_order_payload", lambda *a: {"prod_idx": a[2]})
    monkeypatch.setattr(engine, "_complete_checkout", lambda *a: (True, "CONFIRMED-001"))
    return engine, session


@pytest.mark.parametrize("alias,at,phone", [
    ("행복", "17:50", "01000000001"), ("상자", "20:30", "01000000001"),
    ("상자", "17:30", "01000000002"), ("행복", "20:50", "01000000002")])
def test_four_targets_wait_for_publication_and_keep_exact_identity(monkeypatch, alias, at, phone):
    polls, submissions = [], []
    def fetch(session, branch, theme, day, *a, **k):
        polls.append((theme, day))
        return [] if len(polls) < 3 else [Slot(at, "42", True)]
    engine, _ = prepare(monkeypatch, fetch)
    monkeypatch.setattr(engine, "_add_order", lambda *a: (submissions.append(a[2]) or "ORDER", "SUCCESS"))
    engine.make_reservation_thread(request_data(alias, at, phone))
    assert polls == [(alias, "2026-09-12")] * 3
    assert submissions == [{"prod_idx": "42"}]
    assert engine._success_fired


def test_available_duplicate_time_is_selected(monkeypatch):
    engine, _ = prepare(monkeypatch, lambda *a, **k: [Slot("17:50", "12", False), Slot("17:50", "13", True)])
    submitted = []
    monkeypatch.setattr(engine, "_add_order", lambda *a: (submitted.append(a[2]) or "ORDER", "SUCCESS"))
    engine.make_reservation_thread(request_data())
    assert submitted == [{"prod_idx": "13"}]


def test_lost_order_response_is_not_replayed_and_survives_restart(monkeypatch, tmp_path):
    engine, _ = prepare(monkeypatch, lambda *a, **k: [Slot("17:50", "13", True)])
    engine._journal = OrderJournal(request_data(), tmp_path)
    calls = []
    def lost(*a):
        calls.append(a)
        raise requests.Timeout("accepted by server, response lost")
    monkeypatch.setattr(engine, "_add_order", lost)
    engine.make_reservation_thread(request_data())
    assert len(calls) == 1
    assert engine._order_claimed.is_set() and engine.stop_event.is_set()
    restarted = OrderJournal(request_data(), tmp_path)
    assert restarted.read()["state"] == "unknown"
    assert not restarted.claim()


def test_initial_home_timeout_recovers(monkeypatch):
    engine, session = prepare(monkeypatch, lambda *a, **k: [Slot("17:50", "13", True)])
    calls = []
    def get(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise requests.Timeout()
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None)
    session.get = get
    monkeypatch.setattr(engine, "_add_order", lambda *a: ("ORDER", "SUCCESS"))
    engine.make_reservation_thread(request_data())
    assert len(calls) == 2 and engine._success_fired


def test_native_calendar_can_win_when_legacy_never_publishes(monkeypatch):
    def legacy(*a, **k):
        pytest.fail("fresh native slot should enter the fast path without another legacy request")
    engine, _ = prepare(monkeypatch, legacy)
    ready = Event(); ready.set()
    engine._warm_checkout = SimpleNamespace(ready=ready, error="", native_slot="88", native_seen_at=time.monotonic())
    calls = []
    monkeypatch.setattr(engine, "_add_order", lambda *a: (calls.append(a[2]) or "ORDER", "SUCCESS"))
    engine.make_reservation_thread(request_data())
    assert calls == [{"prod_idx": "88"}]


def test_reentrant_start_preserves_claim_and_workers():
    engine = DpsnnnEngine(lambda *a: None)
    engine.is_running = True
    engine._order_claimed.set()
    engine._next_worker_index = 4
    engine.start_reservation(request_data(), 4)
    assert engine._order_claimed.is_set()
    assert engine._next_worker_index == 4


@pytest.mark.parametrize("badge", ["대", "입금대기", "알수없음", ""])
def test_unknown_and_pending_rows_are_not_available(badge):
    html = f'<div class="booking_list"><a href="?idx=1&day=20260912"><span class="text">행복 / 17:50</span><span class="badge">{badge}</span></a></div>'
    assert not parse_dpsnnn_calendar(html, "2026-09-12", "행복")[0].available


@pytest.mark.parametrize("body,url", [
    ("예약 실패", "https://www.dpsnnn.com/shop_payment_complete/"),
    ("", "https://www.dpsnnn.com/shop_payment_complete/"),
    ("주문이 정상적으로 접수되었습니다.", "https://other.example/shop_payment_complete/"),
    ("주문이 정상적으로 접수되었습니다.", "https://www.dpsnnn.com/shop_payment_complete_error/")])
def test_success_needs_correct_origin_path_and_positive_body(body, url):
    assert not DpsnnnEngine._checkout_success(body, url, "https://www.dpsnnn.com/shop_payment/")


@pytest.mark.parametrize("reply", [{}, {"msg": "SUCCESS"}, [], None])
def test_ambiguous_add_order_response_raises(reply):
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: reply)
    session = SimpleNamespace(post=lambda *a, **k: response)
    engine = DpsnnnEngine(lambda *a: None)
    with pytest.raises(ValueError):
        engine._add_order(session, module.DPSNNN_BRANCHES["gangnam"], {})


def test_journal_claim_is_atomic_and_contains_no_contact(tmp_path):
    journals = [OrderJournal(request_data(), tmp_path) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda journal: journal.claim(), journals))
    assert results.count(True) == 1
    text = journals[0].path.read_text()
    assert "01000000001" not in text and "테스트예약자" not in text
    journals[0].update("created", "ORDER")
    assert journals[1].read()["order_code"] == "ORDER"


def test_independent_reservations_have_independent_journals(tmp_path):
    rows = [request_data("행복", "17:50"), request_data("상자", "20:30"),
            request_data("상자", "17:30", "01000000002"), request_data("행복", "20:50", "01000000002")]
    journals = [OrderJournal(row, tmp_path) for row in rows]
    assert all(j.claim() for j in journals)
    assert len({j.path for j in journals}) == 4


def test_retry_after_blocks_entire_governor():
    governor = ReadGovernor()
    governor.acquire()
    governor.release(SimpleNamespace(status_code=429, headers={"Retry-After": "7"}))
    assert governor.blocked_until - time.monotonic() > 6.9
    assert governor.inflight == 0


def test_preparation_clock_uses_korean_time():
    utc = datetime(2026, 9, 5, 14, 59, 58, tzinfo=timezone.utc)
    assert DpsnnnEngine._payload_prestage_due("2026-09-12", now=utc)


@pytest.mark.parametrize("suffix,expected", [
    ("?idx=1&day=20260912&endDay=20260912", "1"),
    ("?idx=1&day=20260911", ""),
    ("?idx=1&day=20260912&endDay=20260913", ""),
    ("?idx=bad&day=20260912", "")])
def test_native_detail_url_is_bound_to_exact_day(suffix, expected):
    assert detail_slot("https://www.dpsnnn.com/reserve_g" + suffix,
                       "https://www.dpsnnn.com", "/reserve_g", "2026-09-12") == expected


def test_plan_validation_normalizes_times_and_rejects_duplicate_targets(tmp_path):
    path = tmp_path / "private-plan.json"
    rows = [request_data(), request_data("상자", "20:30")]
    rows[0]["reservationTime"] = "17:50"
    path.write_text(json.dumps({"reservations": rows}))
    result = load_plan(path)
    assert result[0]["reservationTime"] == "17:50:00" and result[0]["devMode"] is False
    path.write_text(json.dumps({"reservations": [rows[0], rows[0]]}))
    with pytest.raises(ValueError, match="중복"):
        load_plan(path)


def test_prepared_payload_rejects_wrong_end_day():
    payload = {"prod_idx": "1", "start_day": "2026-09-12", "end_day": "2026-09-13",
               "start_timestamp": "1789138800", "end_timestamp": "1789225200"}
    assert not DpsnnnEngine._prepared_payload_usable(payload, "1", 0, date_str="2026-09-12", now_monotonic=0)


def test_stop_during_detail_preparation_does_not_create_order(monkeypatch):
    engine, _ = prepare(monkeypatch, lambda *a, **k: [Slot("17:50", "13", True)])
    def build(*a):
        engine.stop_event.set()
        return {"prod_idx": "13"}
    monkeypatch.setattr(engine, "_build_order_payload", build)
    monkeypatch.setattr(engine, "_add_order", lambda *a: pytest.fail("must not submit after stop"))
    engine.make_reservation_thread(request_data())


@pytest.mark.parametrize("status,message,expected", [(200, "SUCCESS", True),
    (500, "SUCCESS", False), (200, "", False), (200, "FAIL", False)])
def test_checkout_requires_successful_precheck_and_real_receipt(monkeypatch, status, message, expected):
    engine = DpsnnnEngine(lambda *a: None)
    calls = []
    final = SimpleNamespace(status=200, url="https://www.dpsnnn.com/backpg/payment/booking/index.cm",
                            text=lambda: '{"order_no":"RESERVATION-123"}')
    check = SimpleNamespace(status=status, json=lambda: {"msg": message})
    class Expect:
        value = check
        def __enter__(self): return self
        def __exit__(self, *a): pass
    class Page:
        url = ""
        handlers = {}
        def goto(self, url, **k): self.url = url; return SimpleNamespace(status=200)
        def wait_for_selector(self, *a, **k): pass
        def locator(self, selector):
            return SimpleNamespace(count=lambda: 0,
                inner_text=lambda **k: "주문이 정상적으로 접수되었습니다. 주문 번호 RESERVATION-123")
        def on(self, event, callback): self.handlers[event] = callback
        def expect_response(self, *a, **k): return Expect()
        def wait_for_timeout(self, *a): pass
    page = Page()
    def click():
        calls.append("click")
        page.handlers["response"](final)
        page.url = "https://www.dpsnnn.com/shop_payment_complete/"
    monkeypatch.setattr(engine, "_prepare_orderer_checkout", lambda *a: (True, ""))
    monkeypatch.setattr(engine, "_prepare_cash_checkout", lambda *a: (True, ""))
    monkeypatch.setattr(engine, "_find_checkout_submit", lambda *a: SimpleNamespace(click=click))
    result = engine._checkout_on_page(page, SimpleNamespace(), SimpleNamespace(cookies=[]),
        module.DPSNNN_BRANCHES["gangnam"], "INTERNAL-ORDER", request_data(), "worker")
    assert result[0] is expected
    assert calls == ["click"]
    if expected:
        assert result[1] == "RESERVATION-123"


def test_native_calendar_does_not_click_disabled_future_day():
    class Root:
        def count(self): return 1
        def locator(self, selector): return self
        def get_by_role(self, *a, **k): return self
        def is_enabled(self): return False
        def click(self, **k): pytest.fail("must not force a closed date")
    page = SimpleNamespace(locator=lambda *a: Root())
    warm = WarmCheckout(module.DPSNNN_BRANCHES["gangnam"], request_data(), lambda *a: None, Event())
    assert warm._observe_calendar(page) == "closed"


def test_native_navigation_requires_confirmed_exact_date():
    warm = WarmCheckout(module.DPSNNN_BRANCHES["gangnam"], request_data(), lambda *a: None, Event())
    warm._native_navigation_at = time.monotonic()
    page = SimpleNamespace(url="https://www.dpsnnn.com/reserve_g?idx=7&day=20260911")
    assert warm._observe_calendar(page) == "loading" and warm.native_slot == ""
    page.url = "https://www.dpsnnn.com/reserve_g?idx=8&day=20260912&endDay=20260912"
    assert warm._observe_calendar(page) == "detail" and warm.native_slot == "8"


def test_checkout_selector_accepts_price_and_accessible_submit(monkeypatch):
    candidate = SimpleNamespace(inner_text=lambda: "56,000원 결제하기", is_visible=lambda: True,
                                is_enabled=lambda: True)
    page = SimpleNamespace(locator=lambda s: SimpleNamespace(count=lambda: 1, nth=lambda i: candidate))
    assert DpsnnnEngine._find_checkout_submit(page) is candidate


def test_four_engines_keep_other_bookings_running_after_one_lost_response(monkeypatch):
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None)
    monkeypatch.setattr(module, "create_dpsnnn_session", lambda: SimpleNamespace(
        get=lambda *a, **k: response, close=lambda: None, cookies=[]))
    monkeypatch.setattr(module, "fetch_exact_dpsnnn_slots", lambda session, branch, alias, *a, **k:
        [Slot("17:50", "1", True), Slot("20:50", "2", True)] if alias == "행복" else
        [Slot("17:30", "3", True), Slot("20:30", "4", True)])
    rows = [request_data(), request_data("상자", "20:30"),
            request_data("상자", "17:30", "01000000002"), request_data("행복", "20:50", "01000000002")]
    engines, submissions = [], []
    for index in range(4):
        engine = DpsnnnEngine(lambda *a: None)
        monkeypatch.setattr(engine, "_build_order_payload", lambda *a: {"prod_idx": a[2]})
        def submit(*a, i=index):
            submissions.append(i)
            if i == 0:
                raise requests.Timeout()
            return f"ORDER-{i}", "SUCCESS"
        monkeypatch.setattr(engine, "_add_order", submit)
        monkeypatch.setattr(engine, "_complete_checkout", lambda *a: (True, "CONFIRMED-123"))
        engines.append(engine)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(engine.make_reservation_thread, row) for engine, row in zip(engines, rows)]
        for future in futures:
            future.result(timeout=5)
    assert sorted(submissions) == [0, 1, 2, 3]
    assert [engine._success_fired for engine in engines] == [False, True, True, True]


def test_governor_wait_is_cancellable_without_sending():
    governor = ReadGovernor()
    governor.blocked_until = time.monotonic() + 100
    stop = Event(); stop.set()
    with pytest.raises(requests.RequestException, match="stopped"):
        governor.acquire(priority=True, stop_event=stop)
    assert governor.inflight == 0 and governor.priority_waiters == 0
