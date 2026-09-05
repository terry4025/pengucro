import multiprocessing
import os
import time
from types import SimpleNamespace
from threading import Event

import pytest
import requests
from engines.dpsnnn_shared import SharedReadGovernor


def _budget_worker(root, start):
    os.environ['PENGUCRO_DATA_DIR'] = root
    governor = SharedReadGovernor('test.example')
    governor.LIMIT = 2
    governor.INTERVAL = .005
    while time.time() < start:
        time.sleep(.01)
    periods = []
    for _ in range(2):
        governor.acquire()
        begin = time.monotonic()
        time.sleep(.08)
        end = time.monotonic()
        governor.release(SimpleNamespace(status_code=200))
        periods.append((begin, end))
    return periods


def test_four_processes_share_one_budget_and_all_progress(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    with ctx.Pool(4) as pool:
        periods = pool.starmap(_budget_worker, [(str(tmp_path), time.time()+1)]*4)
    events = sorted((stamp, change) for worker in periods for begin,end in worker
                    for stamp,change in ((begin,1),(end,-1)))
    active = peak = 0
    for _, change in events:
        active += change
        peak = max(peak, active)
    assert peak == 2
    assert all(len(worker)==2 for worker in periods)


def test_backoff_shared_and_cancel_removes_waiter(tmp_path, monkeypatch):
    monkeypatch.setenv('PENGUCRO_DATA_DIR', str(tmp_path))
    first = SharedReadGovernor('test.example')
    second = SharedReadGovernor('test.example')
    first.acquire()
    first.release(SimpleNamespace(status_code=429, headers={'Retry-After':'7'}))
    with second._db() as db:
        assert db.execute('SELECT blocked FROM budget').fetchone()[0] > time.time()+6
    stop = Event(); stop.set()
    with pytest.raises(requests.RequestException):
        second.acquire(stop_event=stop)
    with second._db() as db:
        assert db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0] == 0


def test_dead_process_releases_budget(tmp_path, monkeypatch):
    monkeypatch.setenv('PENGUCRO_DATA_DIR', str(tmp_path))
    governor = SharedReadGovernor('test.example')
    governor.LIMIT = 1
    with governor._db() as db:
        db.execute('INSERT INTO tickets VALUES (?,?,?,?,1)', ('dead',99999999,0,0))
    governor.acquire()
    governor.release(SimpleNamespace(status_code=200))
