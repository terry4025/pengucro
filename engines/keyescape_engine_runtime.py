"""Production Keyescape runtime wiring.

The historical base engine constructs page workers with its own concrete class.
That is fine for the base implementation, but it would silently drop reliability
and network-observability overrides from subclasses.  This final thin layer keeps
all page workers on the active runtime class while preserving the base worker
state-sharing contract.
"""

from __future__ import annotations

import time

from engines.keyescape_engine_observed import KeyescapeEngine as _ObservedKeyescapeEngine
from engines.keyescape_engine_single_page import (
    KeyescapeEngine as _ReliabilityKeyescapeEngine,
)


class KeyescapeEngine(_ObservedKeyescapeEngine):
    """Observed Keyescape runtime whose standby workers keep the same class."""

    def _sync_server_clock(self, announce=False):
        """Preserve a recent precise real sample if a re-sync becomes coarser.

        This production override bypasses the observational parent's snapshot
        restoration so preserving a mapping cannot accidentally refresh the age
        of the original boundary sample.  A precise mapping is therefore kept
        only while its real Date-boundary observation is genuinely recent.
        """
        before_anchor_monotonic = getattr(self.clock, "_anchor_monotonic", None)
        before_anchor_server = getattr(self.clock, "_anchor_server", None)
        before_intervals = list(getattr(self.clock, "_mapping_intervals", []) or [])
        try:
            before_precision = float(self.clock.last_precision)
        except (TypeError, ValueError):
            before_precision = None
        try:
            before_offset = float(self.clock.last_offset)
        except (TypeError, ValueError):
            before_offset = None

        recent_precise = False
        if before_precision is not None and before_precision < 0.5 and before_intervals:
            try:
                newest_real_sample = max(float(sample[2]) for sample in before_intervals)
                age = time.monotonic() - newest_real_sample
                recent_precise = 0.0 <= age <= self.CLOCK_PRECISE_MAX_AGE_SECONDS
            except (TypeError, ValueError, IndexError):
                recent_precise = False

        ok = _ReliabilityKeyescapeEngine._sync_server_clock(
            self, announce=announce
        )

        try:
            measured_precision = float(self.clock.last_precision)
        except (TypeError, ValueError):
            measured_precision = None
        try:
            measured_offset = float(self.clock.last_offset)
        except (TypeError, ValueError):
            measured_offset = None

        preserved = False
        if (
            ok
            and recent_precise
            and measured_precision is not None
            and before_precision is not None
        ):
            regression_threshold = max(
                before_precision * self.CLOCK_REGRESSION_RATIO,
                before_precision + self.CLOCK_REGRESSION_ABSOLUTE_SECONDS,
            )
            if measured_precision > regression_threshold:
                self.clock._anchor_monotonic = before_anchor_monotonic
                self.clock._anchor_server = before_anchor_server
                self.clock.last_precision = before_precision
                self.clock.last_offset = before_offset
                self.clock._mapping_intervals = before_intervals
                preserved = True

        try:
            final_precision = float(self.clock.last_precision)
        except (TypeError, ValueError):
            final_precision = measured_precision
        try:
            final_offset = float(self.clock.last_offset)
        except (TypeError, ValueError):
            final_offset = measured_offset

        if not announce and getattr(self, "_page_index", 1) == 1:
            remaining = None
            if self.open_at is not None:
                try:
                    remaining = self.clock.seconds_until(self.open_at)
                except Exception:
                    remaining = None
            if (
                remaining is not None
                and -1.0 <= remaining <= self.FINAL_SYNC_LEAD + 2.0
            ):
                before_ms = (
                    f"{before_precision * 1000.0:.1f}"
                    if before_precision is not None else "?"
                )
                final_ms = (
                    f"{final_precision * 1000.0:.1f}"
                    if final_precision is not None else "?"
                )
                if before_offset is not None and final_offset is not None:
                    offset_text = f"{(final_offset - before_offset) * 1000.0:+.1f}ms"
                else:
                    offset_text = "확인 불가"
                mode = (
                    "새 측정이 더 거칠어 기존 정밀 매핑 유지"
                    if preserved else
                    "최신 측정 반영"
                )
                self.log(
                    "[정보] 최종 서버 시각 보정 · 정밀도 "
                    f"{before_ms}→{final_ms}ms · 오프셋 변화 {offset_text} · {mode}",
                    "info",
                )
        return ok

    def _make_page_worker(self, page_index):
        worker = self.__class__(
            log_callback=self.log_callback,
            success_callback=None,
            site_url=self.site_url,
        )
        worker.stop_event = self.stop_event
        worker.listener_stop = self.listener_stop
        worker.clock = self.clock
        worker.open_at = self.open_at
        worker._page_index = page_index
        worker._page_count = self._page_count
        worker._page_success_event = self._page_success_event
        worker._live_slot_state = self._live_slot_state
        worker._cancel_watch_state = self._cancel_watch_state
        # The coordinator warms the actual page after workers are constructed.
        # Read its current snapshot; copying the initial dict misses reassignment.
        worker._browser_prewarm_source = self
        worker._trusted_slot_id = (
            self._trusted_slot_id if page_index == 1 else ""
        )
        worker._trusted_slot_sources = self._trusted_slot_sources
        worker._clock_sync_enabled = page_index == 1
        worker._open_at_update_callback = self._set_shared_open_at
        prefix = f"[{page_index}번 페이지]"

        worker.log = lambda message, log_type="info": self.log(
            f"{prefix} {message}", log_type
        )
        worker.silent_tick = lambda message: self.silent_tick(
            f"{prefix} {message}"
        )

        def notify(result=None):
            won = self.notify_success(result)
            self._page_success_event.set()
            if won:
                self._winner_page = page_index
            return won

        worker.notify_success = notify
        return worker
