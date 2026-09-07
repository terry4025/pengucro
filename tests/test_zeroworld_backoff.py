import asyncio
import time
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import engines.async_hot_path as hot_path
from engines.async_hot_path import AsyncHotPathScheduler
from engines.base_engine import BaseEngine
from engines.registry import EngineRegistry
from engines.zeroworld_shin_engine import ZeroWorldAuthenticationRequired
from pengucro.models import BookingResult, STANDARD_MODE


PAYLOAD = dict(
    branch='5', reservationDate='2026-09-20', reservationTime='13:40',
    themePK='9', theme_name='테스트 테마', people='2',
)


def make_engine():
    return EngineRegistry.create(
        site_name='제로월드', mode=STANDARD_MODE, payload={}, custom_sites={},
        log_callback=lambda *_: None, success_callback=lambda: None,
    )


class Response:
    def __init__(self, status=200, body='', headers=None, on_read=None):
        self.status = status
        self.body = body.encode('utf-8')
        self.headers = headers or {}
        self.on_read = on_read
        self.url = 'https://zero.example/response'
        self.history = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def read(self):
        if self.on_read:
            self.on_read()
        return self.body


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def post(self, url, data=None, **_):
        self.requests.append(('POST', url, data))
        return next(self.responses)

    def get(self, url, **_):
        self.requests.append(('GET', url, None))
        return next(self.responses)


@pytest.mark.parametrize('site_name,custom_sites,expected', [
    ('제로월드', {}, 60),
    ('신비월드', {'신비월드': {'engine_id': 'sinbiworld', 'url': 'https://zero.example'}}, 60),
    ('지구별방탈출', {}, 30),
])
def test_real_registry_run_uses_engine_specific_retry_policy(monkeypatch, site_name, custom_sites, expected):
    engine = EngineRegistry.create(
        site_name=site_name, mode=STANDARD_MODE, payload={}, custom_sites=custom_sites,
        log_callback=lambda *_: None, success_callback=lambda: None,
    )
    observed = []

    async def worker(*_):
        observed.append(engine.async_request_scheduler.observe_response(
            429, 100, {'Retry-After': '60'},
        ))

    monkeypatch.setattr(engine, 'pre_fetch_sessions_async', AsyncMock())
    monkeypatch.setattr(engine, 'make_reservation_async_task', worker)
    asyncio.run(BaseEngine.run_async_tasks(engine, PAYLOAD, 1))
    assert observed == [expected]


@pytest.mark.parametrize('raw', ['nan', 'inf', '-inf', 'bad-date', '-5', '0', None])
def test_uncapped_policy_rejects_invalid_infinite_or_negative_delays(raw):
    scheduler = AsyncHotPathScheduler(32, max_retry_after_seconds=None)
    assert scheduler.observe_response(503, 100, {'Retry-After': raw}) == pytest.approx(0.1)


def test_retry_after_http_date_is_not_truncated(monkeypatch):
    monkeypatch.setattr(hot_path, 'time', SimpleNamespace(time=lambda: 1_800_000_000, monotonic=lambda: 100))
    scheduler = AsyncHotPathScheduler(32, max_retry_after_seconds=None)
    delay = scheduler.observe_response(503, 100, {'Retry-After': formatdate(1_800_000_120, usegmt=True)})
    assert delay == 120
    assert scheduler._blocked_until == 220


@pytest.mark.parametrize('status', [429, 503])
def test_calendar_failure_observed_and_healthy_inflight_reply_cannot_clear_cooldown(status):
    engine = make_engine()
    engine.async_request_scheduler = AsyncHotPathScheduler(32, max_retry_after_seconds=None)
    session = Session(Response(status, "fun_days_select('2026-09-20')", {'Retry-After': '60'}))
    before = time.monotonic()
    assert not asyncio.run(engine._wait_for_date(session, engine._build_context(PAYLOAD)))
    deadline = engine.async_request_scheduler._blocked_until
    assert deadline >= before + 60
    engine.observe_async_response(Response(200), 50)
    assert engine.async_request_scheduler._blocked_until == deadline
    assert len(session.requests) == 1


def test_failed_theme_list_does_not_send_followup_theme_select():
    engine = make_engine()
    engine.async_request_scheduler = AsyncHotPathScheduler(32, max_retry_after_seconds=None)
    session = Session(Response(429, headers={'Retry-After': '60'}))
    assert not asyncio.run(engine._prestage_session(session, engine._build_context(PAYLOAD)))
    assert [data['act'] for _, _, data in session.requests] == ['theme_list']


def test_normal_prestaging_takes_one_permit_per_http_request(monkeypatch):
    engine = make_engine()
    wait = AsyncMock(return_value=0)
    monkeypatch.setattr(engine, 'wait_async_scan_turn', wait)
    session = Session(Response(), Response())
    assert asyncio.run(engine._prestage_session(session, engine._build_context(PAYLOAD)))
    assert wait.await_count == len(session.requests) == 2


def test_32_waiting_requests_stop_without_sending_during_server_cooldown():
    async def run():
        engine = make_engine()
        engine.async_request_scheduler = AsyncHotPathScheduler(32, max_retry_after_seconds=None)
        engine.async_request_scheduler.observe_response(429, 100, {'Retry-After': '60'})
        session = Session()
        context = engine._build_context(PAYLOAD)
        tasks = [asyncio.create_task(engine._wait_for_date(session, context)) for _ in range(32)]
        await asyncio.sleep(0.02)
        assert not session.requests
        assert all(not task.done() for task in tasks)
        started = time.monotonic()
        engine.stop_event.set()
        assert await asyncio.wait_for(asyncio.gather(*tasks), timeout=0.5) == [False] * 32
        assert time.monotonic() - started < 0.5
        assert not session.requests
    asyncio.run(run())


def test_late_overload_extends_already_waiting_request_and_then_resumes():
    async def run():
        engine = make_engine()
        scheduler = engine.async_request_scheduler = AsyncHotPathScheduler(32, max_retry_after_seconds=None)
        scheduler._next_dispatch = time.monotonic() + 0.15
        session = Session(Response(200, "fun_days_select('2026-09-20')"))
        task = asyncio.create_task(engine._wait_for_date(session, engine._build_context(PAYLOAD)))
        await asyncio.sleep(0.02)
        scheduler.observe_response(429, 100, {'Retry-After': '60'})
        await asyncio.sleep(0.2)
        assert not session.requests and not task.done()
        # A waiter has retained the longer deadline; stop still interrupts it.
        engine.stop_event.set()
        assert not await asyncio.wait_for(task, timeout=0.5)
        # A new request after the simulated cooldown can proceed normally.
        engine.stop_event.clear()
        scheduler._blocked_until = scheduler._next_dispatch = 0
        assert await engine._wait_for_date(session, engine._build_context(PAYLOAD))
        assert len(session.requests) == 1
    asyncio.run(run())


def test_request_resumes_when_server_cooldown_really_expires():
    async def run():
        engine = make_engine()
        scheduler = engine.async_request_scheduler = AsyncHotPathScheduler(32, max_retry_after_seconds=None)
        scheduler.observe_response(429, 100, {'Retry-After': '0.12'})
        session = Session(Response(200, "fun_days_select('2026-09-20')"))
        started = time.monotonic()
        assert await asyncio.wait_for(
            engine._wait_for_date(session, engine._build_context(PAYLOAD)), timeout=1,
        )
        assert time.monotonic() - started >= 0.10
        assert len(session.requests) == 1
    asyncio.run(run())


def test_stop_during_calendar_reply_prevents_next_slot_request():
    async def run():
        engine = make_engine()
        session = Session(Response(200, "fun_days_select('2026-09-20')", on_read=engine.stop_event.set))
        engine.session_pool = [(session, True, '')]
        engine.async_submission_lock = asyncio.Lock()
        await engine.make_reservation_async_task(PAYLOAD, 0)
        assert [data['act'] for _, _, data in session.requests] == ['calendar']
    asyncio.run(run())


def test_stop_during_auth_session_reply_prevents_next_image_request():
    engine = make_engine()
    session = Session(Response(200, 'a' * 32, on_read=engine.stop_event.set))
    with pytest.raises(ZeroWorldAuthenticationRequired, match='작업 중지'):
        asyncio.run(engine._prepare_captcha(session, '작업 1'))
    assert len(session.requests) == 1


def test_stop_during_final_permit_wait_never_sends_order(monkeypatch):
    engine = make_engine()
    monkeypatch.setattr(engine, '_prepare_captcha', AsyncMock(return_value='synthetic'))
    async def stop_before_permit():
        engine.stop_event.set()
    monkeypatch.setattr(engine, 'wait_async_scan_turn', stop_before_permit)
    session = Session()
    with pytest.raises(ZeroWorldAuthenticationRequired, match='작업 중지'):
        asyncio.run(engine._submit(session, engine._build_context(PAYLOAD), 'LIVE-SLOT'))
    assert not session.requests


def test_registry_32_workers_recover_from_calendar_429_then_submit_live_slot_once(monkeypatch):
    async def run():
        engine = make_engine()
        sent = []
        calendar_count = 0

        class BookingSession:
            def post(self, _url, data, **_):
                nonlocal calendar_count
                sent.append((data['act'], time.monotonic()))
                if data['act'] == 'calendar':
                    calendar_count += 1
                    if calendar_count == 1:
                        return Response(429, headers={'Retry-After': '0.12'})
                    return Response(200, "fun_days_select('2026-09-20')")
                if data['act'] == 'theme_time_list':
                    return Response(200, '<a href="javascript:fun_theme_time_select(\'LIVE-SLOT\')">13:40</a>')
                assert data['act'] == 'theme_time_select'
                return Response()

            async def close(self):
                pass

        async def prepare(count, _payload):
            engine.session_pool = [(BookingSession(), True, '') for _ in range(count)]

        submit = AsyncMock(return_value=BookingResult(True, 'mock confirmed', 'TEST-BOOKING'))
        monkeypatch.setattr(engine, 'pre_fetch_sessions_async', prepare)
        monkeypatch.setattr(engine, '_submit', submit)
        await asyncio.wait_for(BaseEngine.run_async_tasks(engine, PAYLOAD, 32), timeout=3)
        assert len(sent) > 1 and sent[1][1] - sent[0][1] >= 0.10
        assert submit.await_count == 1
        assert submit.await_args.args[2] == 'LIVE-SLOT'
        assert engine._final_submission_state == 'success'
    asyncio.run(run())


@pytest.mark.parametrize('always_expired', [False, True])
def test_long_late_cooldown_refreshes_expired_auth_before_at_most_one_order(monkeypatch, always_expired):
    engine = make_engine()
    calls = []
    async def prepare(session, _worker):
        value = f'mock-{len(calls) + 1}'
        calls.append(value)
        engine._captcha_values[id(session)] = (value, time.monotonic())
        return value
    session = Session(Response(503))
    async def simulate_long_cooldown():
        if len(calls) == 1 or always_expired:
            value, _ = engine._captcha_values[id(session)]
            engine._captcha_values[id(session)] = (value, time.monotonic() - 61)
    monkeypatch.setattr(engine, '_prepare_captcha', prepare)
    monkeypatch.setattr(engine, 'wait_async_scan_turn', simulate_long_cooldown)
    if always_expired:
        with pytest.raises(ZeroWorldAuthenticationRequired, match='예약 요청 미전송'):
            asyncio.run(engine._submit(session, engine._build_context(PAYLOAD), 'LIVE-SLOT'))
        assert not session.requests
    else:
        result = asyncio.run(engine._submit(session, engine._build_context(PAYLOAD), 'LIVE-SLOT'))
        assert not result.success and result.details['outcome'] == 'uncertain'
        assert len(session.requests) == 1
        assert session.requests[0][2]['input_captcha'] == 'mock-2'
    assert len(calls) == 2
