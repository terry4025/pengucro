"""Low-overhead coordination for continuously scanning HTTP engines.

The scheduler does not know or wait for a booking-open timestamp.  It keeps the
configured workers running immediately, but spaces their dispatches across the
measured response cycle so dozens of coroutines do not all resume in one CPU
and server-side burst.  A short shared recovery gate is applied only for
transport failures, HTTP 429 and HTTP 5xx responses.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp


class AsyncHotPathScheduler:
    """Continuously refill up to ``workers`` request slots with phase spacing."""

    INITIAL_RTT_SECONDS = 0.100
    MIN_SPACING_SECONDS = 0.001
    MAX_SPACING_SECONDS = 0.150
    MAX_RECOVERY_DELAY_SECONDS = 2.0
    MAX_RETRY_AFTER_SECONDS = 30.0

    def __init__(
        self,
        workers: int,
        *,
        max_retry_after_seconds: float | None = MAX_RETRY_AFTER_SECONDS,
    ) -> None:
        self.workers = max(1, int(workers))
        # None lets an engine honor the full server-directed cooldown without
        # changing the established policy of other engines using this scheduler.
        self._max_retry_after_seconds = max_retry_after_seconds
        self._dispatch_lock: asyncio.Lock | None = None
        self._next_dispatch = 0.0
        self._blocked_until = 0.0
        self._rtt_ewma = self.INITIAL_RTT_SECONDS
        self._failure_streak = 0
        self._healthy_streak = 0

    @property
    def spacing_seconds(self) -> float:
        return min(
            self.MAX_SPACING_SECONDS,
            max(self.MIN_SPACING_SECONDS, self._rtt_ewma / self.workers),
        )

    async def wait_turn(self, stop_event: Any | None = None) -> float:
        """Wait for the next phase slot and return the actual wait in seconds."""

        if self._dispatch_lock is None:
            self._dispatch_lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with self._dispatch_lock:
            now = started
            dispatch_at = max(now, self._next_dispatch, self._blocked_until)
            self._next_dispatch = dispatch_at + self.spacing_seconds
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            async with self._dispatch_lock:
                if self._blocked_until > dispatch_at:
                    dispatch_at = max(self._blocked_until, self._next_dispatch)
                    self._next_dispatch = dispatch_at + self.spacing_seconds
            remaining = max(0.0, dispatch_at - loop.time())
            if remaining <= 0:
                break
            chunk = min(0.1, remaining)
            await asyncio.sleep(chunk)
        return max(0.0, loop.time() - started)

    def observe_response(
        self,
        status: int,
        rtt_ms: float,
        headers: Mapping[str, Any] | None = None,
    ) -> float:
        """Learn RTT and return a newly applied recovery delay, if any."""

        rtt_seconds = max(0.001, min(10.0, float(rtt_ms) / 1000.0))
        self._rtt_ewma = (self._rtt_ewma * 0.8) + (rtt_seconds * 0.2)
        if status == 429 or 500 <= status <= 599:
            return self._apply_recovery_delay(self._retry_after(headers))

        self._healthy_streak += 1
        if (
            self._healthy_streak >= max(2, min(10, self.workers // 5))
            and time.monotonic() >= self._blocked_until
        ):
            self._failure_streak = 0
        return 0.0

    def observe_network_failure(self) -> float:
        """Temporarily gate new dispatches after a transport-level failure."""

        return self._apply_recovery_delay(None)

    def _apply_recovery_delay(self, requested: float | None) -> float:
        self._healthy_streak = 0
        self._failure_streak = min(8, self._failure_streak + 1)
        exponential = min(
            self.MAX_RECOVERY_DELAY_SECONDS,
            0.1 * math.pow(2.0, self._failure_streak - 1),
        )
        requested_delay = float(requested or 0.0)
        if not math.isfinite(requested_delay):
            requested_delay = 0.0
        requested_delay = max(0.0, requested_delay)
        if self._max_retry_after_seconds is not None:
            requested_delay = min(self._max_retry_after_seconds, requested_delay)
        delay = max(exponential, requested_delay)
        self._blocked_until = max(self._blocked_until, time.monotonic() + delay)
        return delay

    @staticmethod
    def _retry_after(headers: Mapping[str, Any] | None) -> float | None:
        if not headers:
            return None
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            try:
                return max(0.0, parsedate_to_datetime(str(raw)).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None


def create_shared_connector(workers: int) -> aiohttp.TCPConnector:
    """Create one DNS/TLS pool while callers retain independent cookie jars."""

    connections = max(1, int(workers))
    return aiohttp.TCPConnector(
        limit=connections,
        limit_per_host=connections,
        ttl_dns_cache=300,
        keepalive_timeout=60,
    )


def create_isolated_session(
    connector: aiohttp.TCPConnector,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: aiohttp.ClientTimeout | None = None,
) -> aiohttp.ClientSession:
    """Share sockets without sharing cookies or CSRF-bound session state."""

    return aiohttp.ClientSession(
        connector=connector,
        connector_owner=False,
        cookie_jar=aiohttp.CookieJar(),
        headers=dict(headers or {}),
        timeout=timeout,
    )


async def drain_response(response: Any) -> None:
    """Consume or release a response so its keep-alive connection is reusable."""

    try:
        reader = getattr(response, "read", None)
        if reader is not None:
            result = reader()
            if asyncio.iscoroutine(result):
                await result
            return
    except (aiohttp.ClientError, asyncio.TimeoutError, AttributeError, RuntimeError, TypeError):
        reader = None
    try:
        response.release()
    except (AttributeError, RuntimeError, TypeError):
        return
