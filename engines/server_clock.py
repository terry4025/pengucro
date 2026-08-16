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

from pengucro.diagnostics import format_exception


logger = logging.getLogger(__name__)

PIN_POLL_INTERVAL = 0.05
PIN_TIMEOUT_SECONDS = 2.5
REQUEST_TIMEOUT = 5.0
BOUNDARY_HISTORY_SECONDS = 300.0
BOUNDARY_SAMPLE_LIMIT = 5


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
        self.last_read_error = ""
        self._mapping_intervals: list[tuple[float, float, float]] = []

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

    def snapshot(self) -> dict:
        """Return a process-safe clock mapping without cookies or user data."""
        if self._anchor_server is None or self._anchor_monotonic is None:
            return {}
        return {
            "mapping": self._anchor_server - self._anchor_monotonic,
            "precision": float(self.last_precision or 0.5),
            "captured_monotonic": time.monotonic(),
        }

    def apply_snapshot(self, snapshot: dict, max_age: float = 5.0) -> bool:
        """Apply a fresh mapping measured by another local process."""
        if not isinstance(snapshot, dict):
            return False
        try:
            mapping = float(snapshot["mapping"])
            precision = float(snapshot["precision"])
            captured = float(snapshot["captured_monotonic"])
        except (KeyError, TypeError, ValueError):
            return False
        now_mono = time.monotonic()
        age = now_mono - captured
        if age < -1.0 or age > max(0.0, float(max_age)):
            return False
        if not (0.0 < precision <= 1.0):
            return False
        self._anchor_monotonic = now_mono
        self._anchor_server = mapping + now_mono
        self.last_precision = precision
        self.last_offset = self._anchor_server - time.time()
        self._mapping_intervals.append(
            (mapping - precision, mapping + precision, now_mono)
        )
        self._mapping_intervals = self._mapping_intervals[-BOUNDARY_SAMPLE_LIMIT:]
        return True

    def announce_sync(self, shared: bool = False) -> None:
        if not self.synced:
            return
        suffix = " · 다른 실행의 측정 공유" if shared else ""
        self._emit(
            f"서버 시간 동기화 완료 · 로컬 시계와 차이 "
            f"{float(self.last_offset or 0.0):+.2f}초 · 정밀도 약 "
            f"{float(self.last_precision or 0.5) * 1000:.0f}ms{suffix}",
            "success",
        )
        if abs(float(self.last_offset or 0.0)) > 5:
            self._emit(
                f"[경고] 이 PC의 시계가 서버보다 "
                f"{abs(float(self.last_offset or 0.0)):.0f}초 "
                f"{'느립니다' if float(self.last_offset or 0.0) > 0 else '빠릅니다'}. "
                "이후 모든 시각 판단은 서버 시간을 기준으로 합니다.",
                "warning",
            )

    def _apply_boundary_interval(
        self, server: float, lower_monotonic: float, upper_monotonic: float
    ) -> None:
        """Fuse recent second-boundary intervals instead of trusting one RTT."""
        now_mono = time.monotonic()
        lower_mapping = server - upper_monotonic
        upper_mapping = server - lower_monotonic
        self._mapping_intervals.append(
            (lower_mapping, upper_mapping, now_mono)
        )
        self._mapping_intervals = [
            sample for sample in self._mapping_intervals
            if now_mono - sample[2] <= BOUNDARY_HISTORY_SECONDS
        ][-BOUNDARY_SAMPLE_LIMIT:]

        combined_low = max(sample[0] for sample in self._mapping_intervals)
        combined_high = min(sample[1] for sample in self._mapping_intervals)
        if combined_low > combined_high:
            combined_low, combined_high = lower_mapping, upper_mapping
            self._mapping_intervals = [
                (lower_mapping, upper_mapping, now_mono)
            ]
        mapping = (combined_low + combined_high) / 2.0
        self._anchor_monotonic = now_mono
        self._anchor_server = mapping + now_mono
        self.last_precision = max(0.001, (combined_high - combined_low) / 2.0)
        self.last_offset = self._anchor_server - time.time()

    # -- syncing ------------------------------------------------------------
    def _read(self) -> tuple[float, float, float] | None:
        """Return (server_seconds, monotonic_before, monotonic_after)."""
        try:
            before = time.monotonic()
            response = self._session.head(self.url, timeout=REQUEST_TIMEOUT)
            after = time.monotonic()
        except Exception as exc:
            self.last_read_error = f"HEAD 요청 실패 · {format_exception(exc)}"
            return None
        raw = response.headers.get("Date")
        if not raw:
            self.last_read_error = (
                f"HTTP {getattr(response, 'status_code', '확인 불가')} · Date 헤더 없음"
            )
            return None
        try:
            parsed = parsedate_to_datetime(raw).timestamp(), before, after
            self.last_read_error = ""
            return parsed
        except (TypeError, ValueError) as exc:
            self.last_read_error = (
                f"HTTP {getattr(response, 'status_code', '확인 불가')} · "
                f"Date 헤더 해석 실패 · {format_exception(exc)}"
            )
            return None

    def sync(self, announce: bool = False) -> bool:
        """Pin the server's second boundary. Returns True on success."""
        first = self._read()
        if first is None:
            if announce:
                detail = self.last_read_error or "원인 확인 불가"
                self._emit(
                    f"[경고] 서버 시간을 읽지 못했습니다 ({detail}). "
                    "로컬 시계로 진행합니다.",
                    "warning",
                )
            return False

        previous = first[0]
        previous_sample = first
        deadline = time.monotonic() + PIN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            sample = self._read()
            if sample is None:
                break
            server, before, after = sample
            if server > previous:
                # The tick happened after the previous old-second request began
                # and before this new-second response completed. Intersecting
                # several such intervals is both safer and tighter than using
                # one response midpoint plus a fixed polling penalty.
                self._apply_boundary_interval(
                    server, previous_sample[1], after
                )
                if announce:
                    self.announce_sync()
                return True
            previous = server
            previous_sample = sample
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
            reason = (
                f" · 경계 감지 중단 사유 {self.last_read_error}"
                if self.last_read_error
                else " · 제한 시간 내 초 경계 미감지"
            )
            self._emit(
                f"서버 시간 동기화(대략) · 차이 {self.last_offset:+.2f}초 · "
                f"정밀도 약 500ms{reason}",
                "info",
            )
        return True
