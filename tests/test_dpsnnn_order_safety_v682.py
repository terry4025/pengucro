from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from types import SimpleNamespace
import time

import pytest
import requests

from engines import dpsnnn_engine as module
from engines import dpsnnn_runtime as runtime
from engines.dpsnnn_engine import DpsnnnEngine
from engines.dpsnnn_orders import OrderJournal
from engines.dpsnnn_shared import SharedReadGovernor
from engines.zeroworld_catalog import ZeroWorldTimeSlot as Slot


def request_data():
    return {"branch": "gangnam", "themePK": "행복", "reservationDate": "2026-09-12",
            "reservationTime": "17:50:00", "name": "테스트예약자",
            "phone": "01000000001", "depositor": "테스트입금자", "people": "2"}


def reply(data, status=200):
    result = requests.Response()
    result.status_code = status
    result.url = "https://mock-dps.example/add_order.cm"
    result.json = lambda: data
    return result


def prepare(monkeypatch, tmp_path, response_factory, fetch=None):
    logs, posts, checkout = [], [], []
    engine = DpsnnnEngine(lambda message, *args: logs.append(message))
    engine.POLL_INTERVAL = .005
    engine._order_diagnostic_secrets = ("테스트예약자", "테스트입금자", "01000000001")
    engine._journal = OrderJournal(request_data(), tmp_path)

    def post(*args, **kwargs):
        posts.append(time.monotonic())
        return response_factory()

    session = SimpleNamespace(get=lambda *a, **k: reply({}), post=post,
                              close=lambda: None, cookies=[])
    monkeypatch.setattr(module, "create_dpsnnn_session", lambda: session)
    monkeypatch.setattr(module, "fetch_exact_dpsnnn_slots",
                        fetch or (lambda *a, **k: [Slot("17:50", "13", True)]))
    monkeypatch.setattr(engine, "_build_order_payload", lambda *args: {"prod_idx": args[2]})
    monkeypatch.setattr(engine, "_complete_checkout",
                        lambda *args: (checkout.append(args[2]) or True, "CONFIRMED-001"))
    return engine, session, logs, posts, checkout


def test_normalized_success_and_valid_code_reach_receipt_once(monkeypatch, tmp_path):
    engine, _, _, posts, checkout = prepare(
        monkeypatch, tmp_path, lambda: reply({"msg": "  success\n", "order_code": " ORDER-001 "}))
    engine.make_reservation_thread(request_data())
    assert len(posts) == 1
    assert checkout == ["ORDER-001"]
    assert engine._success_fired
    assert engine._journal.read()["state"] == "received"


@pytest.mark.parametrize("timing", [None, {"wait_ms": None, "http_ms": "invalid"}])
def test_invalid_timing_diagnostics_cannot_discard_valid_order_response(monkeypatch, tmp_path, timing):
    engine, session, _, posts, checkout = prepare(
        monkeypatch, tmp_path, lambda: reply({"msg": "SUCCESS", "order_code": "ORDER-001"}))
    session.last_timing = timing
    engine.make_reservation_thread(request_data())
    assert len(posts) == 1
    assert checkout == ["ORDER-001"]
    assert engine._journal.read()["state"] == "received"


@pytest.mark.parametrize("body", [
    {"msg": "FAIL", "order_code": "ORDER-001"},
    {"msg": None, "order_code": "ORDER-001"},
    {"msg": None},
    {"msg": "SUCCESS", "order_code": 123},
    {"msg": "품절", "order_code": 0},
    {"msg": "품절", "order_code": False},
    {"msg": "품절", "order_code": []},
    {"msg": "품절", "order_code": {}},
    {"msg": "예약 필수항목을 확인해주세요"},
    {"msg": "예약 마감 처리 중 오류가 발생했습니다"},
    {"msg": "품절 여부 확인 불가"},
])
def test_ambiguous_or_unclassified_order_is_persisted_and_never_reposted(monkeypatch, tmp_path, body):
    # A second successful response is only a guard against a test hanging if an
    # unsafe retry occurs; the assertion must reject any second POST.
    responses = iter([reply(body), reply({"msg": "SUCCESS", "order_code": "DUPLICATE"})])
    engine, _, logs, posts, checkout = prepare(monkeypatch, tmp_path, lambda: next(responses))
    engine.make_reservation_thread(request_data())
    assert len(posts) == 1
    assert checkout == []
    assert engine.stop_event.is_set() and engine._order_claimed.is_set()
    assert not engine._success_fired
    assert engine._journal.read()["state"] == "unknown"
    assert not OrderJournal(request_data(), tmp_path).claim()
    assert any("재전송" in message or "확인 필요" in message for message in logs)


def test_order_diagnostic_scrubs_contact_name_and_tokens(monkeypatch, tmp_path):
    message = ("<b>테스트예약자</b> 테스트입금자 010-0000-0001 "
               "token=PRIVATE_AUTH access_token=PRIVATE_ACCESS\n필수 항목 확인")
    engine, _, logs, _, _ = prepare(monkeypatch, tmp_path, lambda: reply({"msg": message}))
    engine.make_reservation_thread(request_data())
    rendered = "\n".join(logs)
    for secret in ("테스트예약자", "테스트입금자", "010-0000-0001", "PRIVATE_AUTH", "PRIVATE_ACCESS"):
        assert secret not in rendered
    assert "필수 항목 확인" in rendered
    assert "서버 메시지=" in rendered
    assert "<b>" not in rendered


def test_same_slot_32_workers_submit_unknown_order_only_once(monkeypatch, tmp_path):
    ready = Barrier(32)

    def fetch(*args, **kwargs):
        ready.wait(timeout=5)
        return [Slot("17:50", "13", True)]

    engine, _, _, posts, checkout = prepare(
        monkeypatch, tmp_path, lambda: reply({"msg": "필수항목 오류"}), fetch)
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(engine.make_reservation_thread, request_data()) for _ in range(32)]
        for future in futures:
            future.result(timeout=8)
    assert len(posts) == 1
    assert not checkout
    assert engine._journal.read()["state"] == "unknown"


def test_known_soldout_cooldown_applies_across_workers(monkeypatch, tmp_path):
    responses = iter([reply({"msg": "매진되었습니다."}),
                      reply({"msg": "SUCCESS", "order_code": "ORDER-002"})])
    lock = Lock()

    def next_reply():
        with lock:
            return next(responses)

    engine, _, logs, posts, checkout = prepare(monkeypatch, tmp_path, next_reply)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(engine.make_reservation_thread, request_data()) for _ in range(4)]
        for future in futures:
            future.result(timeout=5)
    assert len(posts) == 2
    assert posts[1] - posts[0] >= 1.0
    assert checkout == ["ORDER-002"]
    assert engine._journal.read()["state"] == "received"
    assert any("최소 1초" in message for message in logs)


def test_worker_rechecks_claim_after_lock_to_avoid_exiting_on_known_rejection(monkeypatch, tmp_path):
    engine, _, _, posts, checkout = prepare(
        monkeypatch, tmp_path, lambda: reply({"msg": "SUCCESS", "order_code": "ORDER-001"}))
    engine._order_claimed.set()
    inner_lock = Lock()

    class ClaimClearedBetweenCheckAndLock:
        first = True

        def acquire(self, *args, **kwargs):
            if self.first:
                self.first = False
                # Another worker just received a definite rejection and cleared
                # its claim before this waiter could acquire the submission lock.
                engine._order_claimed.clear()
            return inner_lock.acquire(*args, **kwargs)

        def release(self):
            inner_lock.release()

    engine.submission_lock = ClaimClearedBetweenCheckAndLock()
    engine.make_reservation_thread(request_data())
    assert len(posts) == 1
    assert checkout == ["ORDER-001"]


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("stage", ["home", "poll"])
def test_access_denied_stops_without_repeated_requests(monkeypatch, tmp_path, status, stage):
    calls = []

    def denied(*args, **kwargs):
        calls.append(stage)
        reply({}, status).raise_for_status()

    engine, session, logs, posts, checkout = prepare(
        monkeypatch, tmp_path, lambda: pytest.fail("must not create order on access denial"))
    if stage == "home":
        session.get = denied
    else:
        monkeypatch.setattr(module, "fetch_exact_dpsnnn_slots", denied)
    engine.make_reservation_thread(request_data())
    assert calls == [stage]
    assert not posts and not checkout
    assert engine.stop_event.is_set()
    assert any(f"HTTP {status} 접근 제한" in message for message in logs)


def test_429_poll_response_does_not_refresh_successful_poll_age(monkeypatch):
    governor = SharedReadGovernor("freshness.example")
    monkeypatch.setitem(runtime._governors, "freshness.example", governor)
    monkeypatch.setattr(requests.Session, "request", lambda *a, **k: reply({}, 429))
    engine = DpsnnnEngine(lambda *a: None)
    before = time.monotonic() - 12
    engine._last_target_poll = before
    session = runtime.DpsnnnSession()
    session.timing_callback = engine._record_request_timing
    result = session.post("https://freshness.example/booking/html_list.cm")
    assert result.status_code == 429
    assert engine._last_target_poll == before


def test_stale_native_slot_requires_fresh_exact_target_query(monkeypatch, tmp_path):
    polls = []

    def fetch(session, branch, theme, day, *args, **kwargs):
        polls.append((theme, day))
        return [Slot("17:50", "NEW-13", True)]

    engine, _, _, posts, checkout = prepare(
        monkeypatch, tmp_path, lambda: reply({"msg": "SUCCESS", "order_code": "ORDER-001"}), fetch)
    ready = Event()
    ready.set()
    engine._warm_checkout = SimpleNamespace(
        ready=ready, error="", native_slot="STALE-88", native_seen_at=time.monotonic() - 3)
    payload_slots = []
    monkeypatch.setattr(engine, "_build_order_payload",
                        lambda *args: (payload_slots.append(args[2]) or {"prod_idx": args[2]}))
    engine.make_reservation_thread(request_data())
    assert polls == [("행복", "2026-09-12")]
    assert payload_slots == ["NEW-13"]
    assert len(posts) == 1 and checkout == ["ORDER-001"]
