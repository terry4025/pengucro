"""Production Keyescape runtime wiring.

The historical base engine constructs page workers with its own concrete class.
That is fine for the base implementation, but it would silently drop reliability
and network-observability overrides from subclasses.  This final thin layer keeps
all page workers on the active runtime class while preserving the base worker
state-sharing contract.
"""

from __future__ import annotations

from engines.keyescape_engine_observed import KeyescapeEngine as _ObservedKeyescapeEngine


class KeyescapeEngine(_ObservedKeyescapeEngine):
    """Observed Keyescape runtime whose standby workers keep the same class."""

    def _make_page_worker(self, page_index):
        worker = self.__class__(
            log_callback=self.log_callback,
            success_callback=None,
            site_url=self.site_url,
        )
        worker.stop_event = self.stop_event
        worker.listener_stop = self.listener_stop
        worker.clock = self.clock
        worker._clock_share = self._clock_share
        worker.open_at = self.open_at
        worker._page_index = page_index
        worker._page_count = self._page_count
        worker._page_success_event = self._page_success_event
        worker._live_slot_state = self._live_slot_state
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
