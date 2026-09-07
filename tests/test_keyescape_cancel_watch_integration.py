"""Cancellation-watch integration through the registered engine; no live site."""
import asyncio
import math
from types import SimpleNamespace

import pytest
import requests

from engines.base_engine import BaseEngine
from engines.keyescape_engine import KeyescapeEngine as BaseKeyescape
from engines.registry import EngineRegistry


DATA = {"keyescape_cancel_watch": True, "devMode": False, "yescaptcha_enabled": False}
TARGET = ("2026-09-12", "20:45", "23", "69")


def row(enabled="Y", at="20:45", slot="live-123"):
    hh, mm = at.split(":")
    return {"num": slot, "hh": hh, "mm": mm, "enable": enabled}


def http_error(status, retry_after=None):
    response = requests.Response()
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    return requests.HTTPError("mock-only", response=response)


@pytest.fixture
def setup(monkeypatch):
    now = [1000.0]
    original_sleep = asyncio.sleep
    async def fast_sleep(seconds, result=None):
        now[0] += max(0.0, seconds)
        await original_sleep(0)
        return result
    monkeypatch.setattr("engines.keyescape_engine.time.monotonic", lambda: now[0])
    monkeypatch.setattr("engines.keyescape_engine.asyncio.sleep", fast_sleep)
    logs = []
    engine = EngineRegistry.create(site_name="키이스케이프", mode="", payload={}, custom_sites={},
        log_callback=lambda *args: logs.append(args), success_callback=None)
    engine._configure_cancel_watch(DATA)
    engine.open_at = None
    engine._clock_sync_enabled = False
    engine._ensure_yescaptcha_token = lambda *args: None
    async def not_blocked(page):
        return False
    async def manual_token(page):
        return "user-solved-token"
    async def front():
        return None
    async def reset(page):
        return None
    engine._is_blocked = not_blocked
    engine._captcha_token_value = manual_token
    engine._reset_captcha_widget = reset
    page = SimpleNamespace(bring_to_front=front)
    submitted = []
    async def submit(page, state, slot_id, *args, **kwargs):
        submitted.append((slot_id, now[0]))
        return "success"
    engine._submit = submit
    return engine, page, now, submitted, logs


def run_watch(engine, page):
    return asyncio.run(engine._watch_and_submit(page, engine._new_submission_state(), dict(DATA),
        *TARGET, "sample-theme", "9999"))


@pytest.mark.parametrize("result", ["success", "capacity", "submission_uncertain", "retry", "captcha_not_ready"])
def test_real_loop_n_n_y_uses_exact_live_slot_and_only_one_final_attempt(setup, result):
    e, p, now, submitted, _ = setup
    responses = iter([[row("N")], [row("Y", at="19:00")], [row("Y")]])
    reads = []
    e._trusted_slot_id = "old-template"
    e._live_slot_state = {"slot_id": "old-cache", "status": "capacity"}
    async def fetch(date, branch, theme):
        assert (date, branch, theme) == (TARGET[0], TARGET[2], TARGET[3])
        reads.append(now[0])
        return next(responses)
    async def submit(page, state, slot_id, *args, **kwargs):
        submitted.append((slot_id, now[0]))
        return result
    e._fetch_slots = fetch
    e._submit = submit
    run_watch(e, p)
    assert len(reads) == 3 and submitted[0][0] == "live-123" and len(submitted) == 1
    assert all(b - a >= 1 - 1e-8 for a, b in zip(reads, reads[1:]))
    assert e._cancel_watch_state["submitted"] is True


def test_ready_observation_is_not_cached_while_waiting_for_manual_token(setup):
    e, p, now, submitted, _ = setup
    reads = []
    async def fetch(*args):
        reads.append(now[0])
        if len(reads) >= 3:
            e.stop_event.set()
        return [row("Y" if len(reads) == 1 else "N")]
    async def token(page):
        return "" if not reads else "user-solved-token"
    e._fetch_slots, e._captcha_token_value = fetch, token
    run_watch(e, p)
    assert len(reads) == 3 and submitted == []


def test_real_loop_429_honors_retry_after_before_y(setup):
    e, p, now, submitted, _ = setup
    reads = []
    async def fetch(*args):
        reads.append(now[0])
        if len(reads) == 1:
            raise http_error(429, 7)
        return [row()]
    e._fetch_slots = fetch
    run_watch(e, p)
    assert len(reads) == 2 and reads[1] - reads[0] >= 7
    assert len(submitted) == 1


def test_initial_lookup_backoff_is_shared_with_runtime_worker(setup):
    e, p, now, _, _ = setup
    e._record_cancel_watch_error(http_error(503, 60))
    worker = e._make_page_worker(1)
    assert worker._cancel_watch_state is e._cancel_watch_state
    reads = []
    async def fetch(*args):
        reads.append(now[0])
        return [row()]
    worker._fetch_slots = fetch
    assert asyncio.run(worker._resolve_cancel_watch_slot(*TARGET)) == ("", "pending")
    assert not reads
    now[0] += 60
    assert asyncio.run(worker._resolve_cancel_watch_slot(*TARGET)) == ("live-123", "ready")


def test_retry_after_long_wait_does_not_issue_periodic_clock_requests(setup):
    e, p, now, submitted, _ = setup
    e._clock_sync_enabled = True
    clock_requests = []
    e._sync_server_clock = lambda **kwargs: clock_requests.append(now[0])
    reads = []
    async def fetch(*args):
        reads.append(now[0])
        if len(reads) == 1:
            raise http_error(429, 180)
        return [row()]
    e._fetch_slots = fetch
    run_watch(e, p)
    assert len(reads) == 2 and reads[1] - reads[0] >= 180
    assert clock_requests == [] and len(submitted) == 1


@pytest.mark.parametrize("retry_after", ["NaN", "Infinity", "bad"])
def test_invalid_retry_after_is_finite_and_backoff_does_not_spin(setup, retry_after):
    e, _, now, *_ = setup
    e._record_cancel_watch_error(http_error(429, retry_after))
    assert math.isfinite(e._cancel_watch_state["next_probe"])
    assert e._cancel_watch_state["next_probe"] - now[0] == 1


def test_forbidden_read_stops_without_submit(setup):
    e, p, _, submitted, _ = setup
    reads = []
    async def fetch(*args):
        reads.append(1)
        raise http_error(403)
    e._fetch_slots = fetch
    run_watch(e, p)
    assert reads == [1] and submitted == [] and e._cancel_watch_state["terminal"]


def test_deadline_is_one_time_600_seconds_and_y_arriving_late_never_submits(setup):
    e, p, now, submitted, _ = setup
    e._start_cancel_watch_deadline()
    deadline = e._cancel_watch_state["deadline"]
    now[0] += 590
    e._start_cancel_watch_deadline()
    assert e._cancel_watch_state["deadline"] == deadline == 1600
    async def fetch(*args):
        now[0] = 1601
        return [row()]
    e._fetch_slots = fetch
    run_watch(e, p)
    assert submitted == [] and e._cancel_watch_state["terminal"]


def test_stop_during_inflight_read_never_submits_returned_y(setup):
    e, p, _, submitted, _ = setup
    async def fetch(*args):
        e.stop_event.set()
        return [row()]
    e._fetch_slots = fetch
    run_watch(e, p)
    assert submitted == []


def test_watch_mode_only_forces_one_page_and_normal_mode_preserved(monkeypatch):
    monkeypatch.setattr(BaseEngine, "start_reservation", lambda *args, **kwargs: None)
    e = BaseKeyescape(lambda *args: None)
    e.start_reservation(dict(DATA), 3)
    assert e._page_count == 1
    e.start_reservation({"keyescape_cancel_watch": False}, 3)
    assert e._page_count == 3 and e._cancel_watch_state is None


def test_real_submit_ambiguous_click_exception_is_not_retryable(setup):
    e, p, _, _, _ = setup
    del e._submit
    async def click(*args):
        raise RuntimeError("lost response after browser click")
    e._run_final_click_action = click
    result = asyncio.run(e._submit(p, e._new_submission_state(), "live-123", "sample-theme",
        TARGET[0], TARGET[1], TARGET[2], False))
    assert result == "submission_uncertain"


@pytest.mark.parametrize("request_started,expected", [(True, "submission_uncertain"), (False, "retry")])
def test_normal_mode_click_exception_is_uncertain_only_after_post_was_observed(setup, request_started, expected):
    e, p, _, _, _ = setup
    del e._submit
    e._cancel_watch_state = None
    state = e._new_submission_state()
    async def click(*args):
        state["request_started"] = request_started
        raise RuntimeError("mock CDP response lost")
    e._run_final_click_action = click
    result = asyncio.run(e._submit(p, state, "live-123", "sample-theme",
        TARGET[0], TARGET[1], TARGET[2], False))
    assert result == expected


def test_ten_minute_loop_expires_without_extending_deadline(setup):
    e, p, now, submitted, _ = setup
    reads = []
    async def fetch(*args):
        reads.append(now[0])
        return [row("N")]
    async def no_token(page):
        return ""
    e._fetch_slots, e._captcha_token_value = fetch, no_token
    run_watch(e, p)
    assert e._cancel_watch_state["deadline"] == 1600
    assert 1600 <= now[0] <= 1600.11 and submitted == []
    assert 1 <= len(reads) <= 601


@pytest.mark.parametrize("opening", [None, 2000])
def test_cancel_mode_rejects_future_or_unknown_opening_before_slot_or_browser_access(setup, opening):
    e, _, _, submitted, _ = setup
    e._sync_server_clock = lambda **kw: True
    async def window(*args):
        return 14, (12, 0)
    e._fetch_window_info = window
    e._resolve_open_moment = lambda *args: opening
    e.clock = SimpleNamespace(seconds_until=lambda _: 1)
    async def forbidden(*args):
        pytest.fail("slot/browser read before verified opening")
    e._fetch_slots = forbidden
    e._open_browser = forbidden
    data = dict(DATA, reservationDate=TARGET[0], reservationTime=TARGET[1],
                branch=TARGET[2], themePK=TARGET[3])
    asyncio.run(e._run_browser_booking_async(data))
    assert submitted == []


@pytest.mark.parametrize("trusted,live,expected", [
    (("2026-09-05", "2026-09-06"), None, "사전 검증 시간표"),
    (("실시간 HTTP 검증",), None, "대상 날짜 실조회"),
    ((), {"slot_id": "live-123", "target_date": TARGET[0], "zizum_num": TARGET[2]}, "대상 날짜 실조회"),
    ((), None, "출처 확인 불가"),
])
def test_submit_log_distinguishes_slot_provenance(setup, trusted, live, expected):
    e, p, _, _, logs = setup
    del e._submit
    e._cancel_watch_state = None
    e._trusted_slot_id = "live-123" if trusted else ""
    e._trusted_slot_sources = trusted
    e._live_slot_state = live
    async def click(*args):
        return {"written": 1, "buttonFound": True, "clicked": True}
    async def no_completion(*args, **kw):
        return None
    e._run_final_click_action, e._await_completion = click, no_completion
    asyncio.run(e._submit(p, e._new_submission_state(), "live-123", "sample-theme",
        TARGET[0], TARGET[1], TARGET[2], False))
    click_logs = [message for message, _ in logs if "예약하기를 클릭" in message]
    assert len(click_logs) == 1 and expected in click_logs[0]
