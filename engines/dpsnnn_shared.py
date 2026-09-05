"""Process-local HTTP scheduling; the legacy class name remains compatible.

No shared database, filesystem tickets, or PID probes. OrderJournal is separate.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import math
import threading
import time
import requests


class ReservationCancelled(requests.RequestException):
    """Normal cancellation, not a network failure."""


@dataclass(eq=False)
class Permit:
    priority: bool
    owner: threading.Thread = field(default_factory=threading.current_thread)
    queued_at: float = field(default_factory=time.monotonic)


class SharedReadGovernor:
    """One host budget per process, with bounded priority bursts."""
    LIMIT = 32
    INTERVAL = 1 / 32

    def __init__(self, host=""):
        self.host = host
        self.condition = threading.Condition()
        self.local = threading.local()
        self._waiting = []
        self._active = set()
        self._priority_streak = 0
        self.next_read = 0.0
        self.blocked_until = 0.0
        self.failures = 0
        self.reclaimed = 0

    @property
    def inflight(self):
        with self.condition:
            return len(self._active)

    @property
    def priority_waiters(self):
        with self.condition:
            return sum(p.priority for p in self._waiting)

    def snapshot(self):
        with self.condition:
            return dict(active=len(self._active), waiting=len(self._waiting),
                        limit=self.LIMIT, reclaimed=self.reclaimed)

    def wake(self):
        with self.condition:
            self.condition.notify_all()

    def _head(self):
        ordinary = next((p for p in self._waiting if not p.priority), None)
        priority = next((p for p in self._waiting if p.priority), None)
        if priority is not None and (ordinary is None or self._priority_streak < 3):
            return priority
        return ordinary

    def acquire(self, priority=False, stop_event=None):
        permit = Permit(bool(priority))
        with self.condition:
            self._waiting.append(permit)
            try:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        raise ReservationCancelled("reservation stopped")
                    # Slow live HTTP threads retain their capacity without a TTL.
                    dead = {p for p in self._active if not p.owner.is_alive()}
                    self._active.difference_update(dead)
                    self.reclaimed += len(dead)
                    now = time.monotonic()
                    delay = max(self.next_read, self.blocked_until) - now
                    if self._head() is permit and len(self._active) < self.LIMIT and delay <= 0:
                        self._waiting.remove(permit)
                        self._active.add(permit)
                        self.next_read = now + self.INTERVAL
                        self._priority_streak = self._priority_streak + 1 if priority else 0
                        self.local.permit = permit
                        self.condition.notify_all()
                        return permit
                    self.condition.wait(max(.001, min(.05, delay)) if delay > 0 else .05)
            finally:
                if permit in self._waiting:
                    self._waiting.remove(permit)
                self.condition.notify_all()

    def abandon(self, permit=None):
        """Idempotent capacity return, independent of response bookkeeping."""
        permit = permit or getattr(self.local, 'permit', None)
        with self.condition:
            self._active.discard(permit)
            if getattr(self.local, 'permit', None) is permit:
                self.local.permit = None
            self.condition.notify_all()

    def release(self, response=None, failed=False, *, permit=None):
        permit = permit or getattr(self.local, 'permit', None)
        with self.condition:
            if permit not in self._active:
                return
            try:
                status = getattr(response, 'status_code', 0)
                if failed or status == 429 or status >= 500:
                    self.failures = min(self.failures + 1, 6)
                    delay = min(8.0, .25 * 2 ** (self.failures - 1))
                    retry = getattr(response, 'headers', {}).get('Retry-After', '')
                    try:
                        seconds = float(retry)
                    except (ValueError, TypeError):
                        try:
                            seconds = parsedate_to_datetime(retry).timestamp() - time.time()
                        except (ValueError, TypeError, OverflowError):
                            seconds = 0.0
                    if math.isfinite(seconds):
                        delay = max(delay, seconds)
                    self.blocked_until = max(self.blocked_until, time.monotonic() + delay)
                elif 200 <= status < 300:
                    self.failures = 0
            finally:
                self.abandon(permit)
