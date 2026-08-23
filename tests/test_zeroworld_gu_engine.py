import asyncio
import json
import time

from engines.zeroworld_gu_engine import ZeroWorldGuEngine


class AsyncResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload
        self.headers = {}
        self.reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self):
        self.reads += 1
        return json.dumps(self.payload).encode("utf-8")

    async def json(self):
        return self.payload

    async def text(self):
        return json.dumps(self.payload)


class AsyncSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.posts = 0
        self.closed = False

    def post(self, *_args, **_kwargs):
        self.posts += 1
        return next(self.responses)

    async def close(self):
        self.closed = True


def reservation_data():
    return {
        "reservationDate": "2026-08-28",
        "reservationTime": "19:05:00",
        "themePK": "4",
        "name": "테스트",
        "phone": "010-0000-0000",
        "people": "2",
    }


def test_server_failure_recovers_and_continuous_scan_resumes():
    async def scenario():
        logs = []
        successes = []
        engine = ZeroWorldGuEngine(
            "https://zero.example",
            lambda message, level: logs.append((message, level)),
            lambda: successes.append(True),
        )
        session = AsyncSession(
            [
                AsyncResponse(503, {"Message": "maintenance"}),
                AsyncResponse(200, {"booking": "ok"}),
            ]
        )

        async def prefetch(_num_sessions, _reservation_data):
            engine.session_pool = [(session, "csrf")]

        engine.pre_fetch_sessions_async = prefetch
        started = time.monotonic()
        await engine.run_async_tasks(reservation_data(), 1)
        return engine, session, logs, successes, time.monotonic() - started

    engine, session, logs, successes, elapsed = asyncio.run(scenario())

    assert session.posts == 2
    assert session.closed is True
    assert successes == [True]
    assert engine.attempt_count == 1
    assert elapsed >= 0.08
    assert any("서버 복구 간격" in message for message, _level in logs)


def test_csrf_expiry_drains_response_and_refreshes_only_that_session():
    async def scenario():
        engine = ZeroWorldGuEngine("https://zero.example", lambda *_args: None)
        expired = AsyncResponse(419, {})
        session = AsyncSession([expired, AsyncResponse(200, {"booking": "ok"})])
        refreshes = 0

        async def prefetch(_num_sessions, _reservation_data):
            engine.session_pool = [(session, "old-csrf")]

        async def refresh(_session):
            nonlocal refreshes
            refreshes += 1
            return "new-csrf"

        engine.pre_fetch_sessions_async = prefetch
        engine.get_csrf_token_async = refresh
        await engine.run_async_tasks(reservation_data(), 1)
        return expired, refreshes

    expired, refreshes = asyncio.run(scenario())

    assert expired.reads == 1
    assert refreshes == 1
