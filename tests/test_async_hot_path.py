import asyncio

from engines.async_hot_path import (
    AsyncHotPathScheduler,
    create_isolated_session,
    create_shared_connector,
)


def test_fifty_worker_scheduler_uses_dense_two_millisecond_initial_phase():
    scheduler = AsyncHotPathScheduler(50)

    assert scheduler.workers == 50
    assert scheduler.spacing_seconds == 0.002


def test_scheduler_honors_server_failure_and_retry_after_with_a_safe_cap():
    scheduler = AsyncHotPathScheduler(50)

    delay = scheduler.observe_response(503, 120.0, {"Retry-After": "1.5"})
    capped = scheduler.observe_response(429, 130.0, {"Retry-After": "60"})

    assert delay == 1.5
    assert capped == scheduler.MAX_RETRY_AFTER_SECONDS


def test_fifty_dispatches_are_phase_spread_instead_of_one_burst():
    async def scenario():
        scheduler = AsyncHotPathScheduler(50)
        loop = asyncio.get_running_loop()

        async def worker():
            await scheduler.wait_turn()
            return loop.time()

        dispatched = await asyncio.gather(*(worker() for _ in range(50)))
        return max(dispatched) - min(dispatched)

    spread = asyncio.run(scenario())

    assert spread >= 0.06
    assert spread < 0.25


def test_sessions_share_connector_but_not_cookie_state():
    async def scenario():
        connector = create_shared_connector(2)
        first = create_isolated_session(connector)
        second = create_isolated_session(connector)
        try:
            assert first.connector is connector
            assert second.connector is connector
            assert first.cookie_jar is not second.cookie_jar
        finally:
            await first.close()
            await second.close()
            await connector.close()

    asyncio.run(scenario())
