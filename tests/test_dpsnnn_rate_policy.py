from datetime import datetime, timezone
from email.utils import format_datetime
from threading import Event, Lock, Thread
from types import SimpleNamespace
import time

import pytest

from engines import dpsnnn_shared
from engines.dpsnnn_shared import ReservationCancelled, SharedReadGovernor


def response(status=200, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return SimpleNamespace(status_code=status, headers=headers)


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=100.0, wall=1_800_000_000.0)
    monkeypatch.setattr(dpsnnn_shared, "time", SimpleNamespace(
        monotonic=lambda: state.now, time=lambda: state.wall))
    return state


def complete(governor, clock, result=None, failed=False):
    clock.now = max(clock.now, governor.next_read, governor.blocked_until) + .001
    token = governor.acquire()
    governor.release(result if result is not None else response(), failed=failed, permit=token)


def test_conservative_http_budget_is_not_worker_count():
    governor = SharedReadGovernor()
    snapshot = governor.snapshot()
    assert snapshot["limit"] == snapshot["base_limit"] == 8
    assert snapshot["rate_per_second"] == 8
    assert snapshot["interval_ms"] == 125


def test_priority_admission_uses_the_same_effective_request_spacing(clock):
    governor = SharedReadGovernor()
    permit = governor.acquire(priority=True)
    assert governor.next_read - clock.now == pytest.approx(.125)
    governor.release(response(429), permit=permit)
    clock.now = governor.blocked_until + .001
    permit = governor.acquire(priority=True)
    assert governor.next_read - clock.now == pytest.approx(.25)
    governor.release(response(), permit=permit)
    assert governor.inflight == 0


@pytest.mark.parametrize("status,failed", [(429, False), (503, False), (0, True)])
def test_overload_reduces_both_http_ceilings_and_does_not_retry_internally(clock, status, failed):
    governor = SharedReadGovernor()
    complete(governor, clock, response(status), failed)
    snapshot = governor.snapshot()
    assert snapshot["limit"] == 4
    assert snapshot["rate_per_second"] == 4
    assert snapshot["backoff_ms"] == pytest.approx(1000)
    assert snapshot["active"] == 0
    complete(governor, clock, response(status), failed)
    assert governor.snapshot()["limit"] == 2
    assert governor.snapshot()["rate_per_second"] == 2
    assert governor.snapshot()["backoff_ms"] == pytest.approx(2000)
    for _ in range(6):
        complete(governor, clock, response(status), failed)
    assert governor.snapshot()["limit"] == 1
    assert governor.snapshot()["rate_per_second"] == 1
    assert governor.snapshot()["backoff_ms"] == pytest.approx(8000)


@pytest.mark.parametrize("retry,minimum", [("7", 7), ("0", 1), ("-1", 1),
                                           ("nonsense", 1), ("nan", 1), ("inf", 1)])
def test_retry_after_never_removes_local_backoff(clock, retry, minimum):
    governor = SharedReadGovernor()
    complete(governor, clock, response(429, retry))
    assert governor.blocked_until - clock.now == pytest.approx(minimum)


def test_retry_after_http_date_is_respected(clock):
    governor = SharedReadGovernor()
    date = format_datetime(datetime.fromtimestamp(clock.wall + 30, timezone.utc), usegmt=True)
    complete(governor, clock, response(503, date))
    assert governor.blocked_until - clock.now == pytest.approx(30)


def test_late_healthy_response_cannot_undo_overload(clock):
    governor = SharedReadGovernor()
    first = governor.acquire()
    clock.now = governor.next_read + .001
    late = governor.acquire()
    governor.release(response(429, "20"), permit=first)
    blocked_until = governor.blocked_until
    clock.now += 30
    governor.release(response(), permit=late)
    assert governor.failures == 1
    assert governor._healthy_successes == 0
    assert governor.snapshot()["pressure"] == 1
    assert governor.blocked_until == blocked_until
    assert governor.inflight == 0


def test_recovery_needs_both_quiet_time_and_healthy_requests_and_is_gradual(clock):
    governor = SharedReadGovernor()
    for _ in range(3):
        complete(governor, clock, response(429))
    assert governor.snapshot()["pressure"] == 3
    # Enough time alone is not evidence that the server is accepting traffic.
    clock.now += 30
    complete(governor, clock)
    assert governor.snapshot()["pressure"] == 3
    for _ in range(15):
        complete(governor, clock)
    assert governor.snapshot()["pressure"] == 2
    assert governor.snapshot()["rate_per_second"] == 2
    # Enough healthy responses alone must not cause an immediate ramp-up.
    for _ in range(16):
        complete(governor, clock)
    assert governor.snapshot()["pressure"] == 2
    clock.now += 15
    complete(governor, clock)
    assert governor.snapshot()["pressure"] == 1
    assert governor.snapshot()["rate_per_second"] == 4
    clock.now += 15
    for _ in range(16):
        complete(governor, clock)
    assert governor.snapshot()["pressure"] == 0
    assert governor.snapshot()["rate_per_second"] == 8
    assert governor.failures == 0


def test_duplicate_release_does_not_double_penalize(clock):
    governor = SharedReadGovernor()
    permit = governor.acquire()
    governor.release(response(429), permit=permit)
    snapshot = governor.snapshot()
    governor.release(response(429), permit=permit)
    assert governor.snapshot() == snapshot
    assert governor.failures == 1


def test_bookkeeping_failure_still_returns_capacity():
    governor = SharedReadGovernor()
    permit = governor.acquire()

    class InvalidResponse:
        @property
        def status_code(self):
            raise RuntimeError("bookkeeping failure")

    with pytest.raises(RuntimeError, match="bookkeeping failure"):
        governor.release(InvalidResponse(), permit=permit)
    assert governor.inflight == 0
    governor.abandon(permit)
    assert governor.inflight == 0


def test_32_workers_complete_under_eight_request_capacity():
    governor = SharedReadGovernor()
    governor.INTERVAL = 0  # Isolate capacity from the separately tested rate.
    lock = Lock()
    seen = []
    active = peak = 0

    def worker(index):
        nonlocal active, peak
        permit = governor.acquire()
        try:
            with lock:
                active += 1
                peak = max(peak, active)
                seen.append(index)
            time.sleep(.02)
        finally:
            with lock:
                active -= 1
            governor.release(response(), permit=permit)

    threads = [Thread(target=worker, args=(index,)) for index in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(seen) == list(range(32))
    assert 1 <= peak <= 8
    assert governor.inflight == 0


def test_priority_remains_bounded_at_reduced_capacity_and_stop_interrupts_backoff():
    governor = SharedReadGovernor()
    governor.INTERVAL = 0
    governor._pressure = 3
    held = governor.acquire()
    order = []

    def worker(priority):
        permit = governor.acquire(priority)
        order.append(priority)
        governor.release(permit=permit)

    threads = [Thread(target=worker, args=(False,))]
    threads += [Thread(target=worker, args=(True,)) for _ in range(5)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2
    while governor.snapshot()["waiting"] < 6 and time.monotonic() < deadline:
        time.sleep(.005)
    governor.release(permit=held)
    for thread in threads:
        thread.join(2)
    assert order[:4] == [True, True, True, False]
    assert not any(thread.is_alive() for thread in threads)

    governor.blocked_until = time.monotonic() + 60
    stop, cancelled = Event(), Event()

    def wait_for_stop():
        try:
            governor.acquire(priority=True, stop_event=stop)
        except ReservationCancelled:
            cancelled.set()

    waiter = Thread(target=wait_for_stop)
    waiter.start()
    started = time.monotonic()
    stop.set()
    governor.wake()
    waiter.join(.3)
    assert cancelled.is_set()
    assert time.monotonic() - started < .3
    assert governor.snapshot()["waiting"] == governor.inflight == 0
