import multiprocessing
import time
from types import SimpleNamespace
from threading import Event, Thread
import pytest
import requests
from engines.dpsnnn_shared import SharedReadGovernor, ReservationCancelled


def _budget_worker(start, ready, output):
    governor = SharedReadGovernor('test.example')
    governor.INTERVAL = 0
    token = governor.acquire()
    ready.put(True)
    start.wait(10)
    governor.release(permit=token)
    output.put(governor.inflight)


def test_four_spawn_processes_are_independent():
    ctx = multiprocessing.get_context('spawn')
    start, ready, output = ctx.Event(), ctx.Queue(), ctx.Queue()
    processes = [ctx.Process(target=_budget_worker, args=(start, ready, output)) for _ in range(4)]
    for process in processes:
        process.start()
    try:
        assert all(ready.get(timeout=15) for _ in processes)
        start.set()
        assert [output.get(timeout=10) for _ in processes] == [0]*4
    finally:
        start.set()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()


@pytest.mark.parametrize('status', [429, 503])
def test_retry_after_and_cancel(status):
    governor = SharedReadGovernor()
    governor.acquire()
    governor.release(SimpleNamespace(status_code=status, headers={'Retry-After':'7'}))
    assert governor.blocked_until > time.monotonic()+6
    stop = Event()
    finished = Event()
    def waiter():
        with pytest.raises(ReservationCancelled):
            governor.acquire(stop_event=stop)
        finished.set()
    thread = Thread(target=waiter)
    thread.start()
    stop.set()
    governor.wake()
    assert finished.wait(.3)
    thread.join()
    assert governor.snapshot()['waiting'] == 0


def test_capacity_dead_owner_and_idempotent_release():
    governor = SharedReadGovernor()
    governor.LIMIT = 1
    governor.INTERVAL = 0
    thread = Thread(target=governor.acquire)
    thread.start()
    thread.join()
    token = governor.acquire()
    assert governor.reclaimed == 1
    governor.release(permit=token)
    governor.release(permit=token)
    assert governor.inflight == 0


def test_actual_session_no_sqlite_and_cleanup_preserves_order(monkeypatch):
    import sqlite3
    from engines import dpsnnn_runtime as runtime
    governor = SharedReadGovernor()
    monkeypatch.setitem(runtime._governors, 'mock.example', governor)
    def forbidden(*args, **kwargs):
        raise AssertionError('no DB in HTTP path')
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    response = SimpleNamespace(status_code=200, json=lambda: {'order_code':'LOCAL'})
    monkeypatch.setattr(requests.Session, 'request', lambda *a, **k: response)
    monkeypatch.setattr(governor, 'release', forbidden)
    session = runtime.DpsnnnSession()
    assert session.post('https://mock.example/add_order.cm') is response
    assert governor.inflight == 0
    assert session.last_timing['http_ms'] >= 0


def test_network_timeout_returned_once_with_backoff(monkeypatch):
    from engines import dpsnnn_runtime as runtime
    governor = SharedReadGovernor()
    monkeypatch.setitem(runtime._governors, 'timeout.example', governor)
    calls = []
    def fail(*a, **k):
        calls.append(1)
        raise requests.Timeout('mock')
    monkeypatch.setattr(requests.Session, 'request', fail)
    with pytest.raises(requests.Timeout):
        runtime.DpsnnnSession().post('https://timeout.example/add_order.cm')
    assert calls == [1]
    assert governor.inflight == 0
    assert governor.blocked_until > time.monotonic()


def test_priority_is_bounded_and_normal_not_starved():
    governor = SharedReadGovernor()
    governor.LIMIT = 1
    governor.INTERVAL = 0
    held = governor.acquire()
    order = []
    def run(priority, label):
        token = governor.acquire(priority)
        order.append(label)
        governor.release(permit=token)
    threads = [Thread(target=run, args=(False, 'normal'))]
    threads += [Thread(target=run, args=(True, 'priority')) for _ in range(5)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic()+2
    while governor.snapshot()['waiting'] < 6 and time.monotonic() < deadline:
        time.sleep(.005)
    governor.release(permit=held)
    for thread in threads:
        thread.join(2)
    assert order[:4] == ['priority']*3+['normal']
    assert governor.inflight == 0
