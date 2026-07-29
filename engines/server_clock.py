"""Track a site's own clock so timing never depends on the local one.

Why this is not optional
------------------------
Measured on the development machine, the local clock sat 37 seconds behind
reality. Four independent hosts (keyescape, google, cloudflare, naver) all
reported the same gap within 0.15 s, so the PC was wrong rather than any one
server being fast. A booking triggered from the local clock would therefore have
fired 37 seconds late -- a guaranteed miss.

How the sub-second boundary is recovered
----------------------------------------
An HTTP ``Date`` header only carries whole seconds, so a single read is accurate
to +/-0.5 s. Polling rapidly and catching the instant the value increments pins
the boundary of that second to roughly the polling interval instead. Measured
spread across repeated pinning runs was 0.02 s.

``HEAD`` is used because it returns no body at all -- 0 bytes and ~20 ms against
keyescape, versus ~17 kB for a GET of the same page.

The offset is anchored to ``time.monotonic()`` rather than to the wall clock, so
an NTP correction (or the user fixing their clock) part-way through a run cannot
shift the computed target.
"""

from __future__ import annotations

import logging
import time
from email.utils import parsedate_to_datetime

import requests


logger = logging.getLogger(__name__)

PIN_POLL_INTERVAL = 0.05
PIN_TIMEOUT_SECONDS = 2.5
REQUEST_TIMEOUT = 5.0


class ServerClock:
    """Reads a server's clock and reports time on that clock."""

    def __init__(self, url: str, session: requests.Session | None = None, log=None):
        self.url = url
        self.log = log
        self._session = session or requests.Session()
        self._anchor_monotonic: float | None = None
        self._anchor_server: float | None = None
        self.last_precision: float | None = None
        self.last_offset: float | None = None

    # -- reporting ----------------------------------------------------------
    def _emit(self, message: str, level: str = "info") -> None:
        if self.log:
            self.log(message, level)
        logger.info(message)

    @property
    def synced(self) -> bool:
        return self._anchor_server is not None

    def now(self) -> float:
        """Server epoch seconds. Falls back to the local clock if unsynced."""
        if self._anchor_server is None or self._anchor_monotonic is None:
            return time.time()
        return self._anchor_server + (time.monotonic() - self._anchor_monotonic)

    def seconds_until(self, target_epoch: float) -> float:
        return target_epoch - self.now()

    # -- syncing ------------------------------------------------------------
    def _read(self) -> tuple[float, float, float] | None:
        """Return (server_seconds, monotonic_before, monotonic_after)."""
        try:
            before = time.monotonic()
            response = self._session.head(self.url, timeout=REQUEST_TIMEOUT)
            after = time.monotonic()
        except Exception:
            return None
        raw = response.headers.get("Date")
        if not raw:
            return None
        try:
            return parsedate_to_datetime(raw).timestamp(), before, after
        except (TypeError, ValueError):
            return None

    def sync(self, announce: bool = False) -> bool:
        """Pin the server's second boundary. Returns True on success."""
        first = self._read()
        if first is None:
            if announce:
                self._emit("[경고] 서버 시간을 읽지 못했습니다. 로컬 시계로 진행합니다.", "warning")
            return False

        previous = first[0]
        deadline = time.monotonic() + PIN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            sample = self._read()
            if sample is None:
                break
            server, before, after = sample
            if server > previous:
                # The tick fell inside this request window; its midpoint is the
                # best estimate of when the new second began.
                midpoint = (before + after) / 2
                self._anchor_server = server
                self._anchor_monotonic = midpoint
                self.last_precision = (after - before) / 2 + PIN_POLL_INTERVAL
                self.last_offset = server - (
                    time.time() - (time.monotonic() - midpoint)
                )
                if announce:
                    self._emit(
                        f"서버 시간 동기화 완료 · 로컬 시계와 차이 "
                        f"{self.last_offset:+.2f}초 · 정밀도 약 "
                        f"{self.last_precision * 1000:.0f}ms",
                        "success",
                    )
                    if abs(self.last_offset) > 5:
                        self._emit(
                            f"[경고] 이 PC의 시계가 서버보다 "
                            f"{abs(self.last_offset):.0f}초 "
                            f"{'느립니다' if self.last_offset > 0 else '빠릅니다'}. "
                            "이후 모든 시각 판단은 서버 시간을 기준으로 합니다.",
                            "warning",
                        )
                return True
            previous = server
            time.sleep(PIN_POLL_INTERVAL)

        # No tick observed: fall back to the coarse estimate, still better than
        # trusting a local clock that may be tens of seconds out.
        server, before, after = first
        self._anchor_server = server + 0.5
        self._anchor_monotonic = (before + after) / 2
        self.last_precision = 0.5
        self.last_offset = self._anchor_server - (
            time.time() - (time.monotonic() - self._anchor_monotonic)
        )
        if announce:
            self._emit(
                f"서버 시간 동기화(대략) · 차이 {self.last_offset:+.2f}초 · "
                "정밀도 약 500ms",
                "info",
            )
        return True
