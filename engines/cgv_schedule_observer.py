"""Single-flight, read-only publication refresh during the seat priority pass."""
from __future__ import annotations

import time
import uuid


def run_schedule_wave(page, script, arguments, stop_event, timeout_seconds):
    """Bound the network wait with the host clock; submit GETs only once."""
    key = uuid.uuid4().hex
    start = """({key, args}) => {
      const entries = window.__pengucroScheduleWaves ||= {};
      const entry = entries[key] = {result: null, cancel: null};
      const run = """ + script + """;
      run({...args, observerKey: key}).then(result => {entry.result = result;})
        .catch(() => {entry.result = {ok: false, status: 0, error: 'schedule-wave-error'};});
      return true;
    }"""
    read = r"""key => {
      const entry = (window.__pengucroScheduleWaves || {})[key];
      return entry ? {present: true, result: entry.result} : {present: false};
    }"""
    cancel = r"""key => {
      const entries = window.__pengucroScheduleWaves || {};
      const entry = entries[key];
      if (entry && entry.cancel) entry.cancel();
      delete entries[key];
    }"""
    deadline = time.monotonic() + timeout_seconds
    try:
        page.evaluate(start, {"key": key, "args": arguments})
        while not stop_event.is_set():
            if time.monotonic() >= deadline:
                return {"ok": False, "status": 0, "timedOut": True,
                        "error": "schedule-host-timeout"}
            state = page.evaluate(read, key)
            if not isinstance(state, dict) or not state.get("present"):
                return {"ok": False, "status": 0, "error": "schedule-wave-lost"}
            if isinstance(state.get("result"), dict):
                return state["result"]
            stop_event.wait(0.01)
        return {"ok": False, "status": 0, "error": "schedule-stopped"}
    finally:
        try:
            page.evaluate(cancel, key)
        except Exception:
            pass


class ScheduleObserver:
    INTERVAL_SECONDS = 1.0
    TIMEOUT_SECONDS = 6.0

    # evaluate returns immediately; it never awaits the network promise. The
    # Python deadline remains useful if Chrome delays in-page timer callbacks.
    # A frozen CDP connection itself is still subject to Playwright's transport.
    STEP_SCRIPT = r"""({key, url, action}) => {
      const states = window.__pengucroScheduleObservers ||= {};
      const previous = states[key];
      if (action === 'cancel') {
        if (previous) previous.controller.abort();
        delete states[key];
        return {state: 'cancelled'};
      }
      if (previous) {
        if (previous.url !== url) return {state: 'mismatch'};
        if (!previous.result) return {state: 'running'};
        delete states[key];
        return {state: 'done', result: previous.result};
      }
      const controller = new AbortController();
      const state = states[key] = {url, controller, result: null};
      const started = performance.now();
      const headers = new Headers({'Accept': 'application/json, text/plain, */*'});
      const cookie = String(document.cookie || '').split('; ')
        .find(value => value.startsWith('accessToken='));
      if (cookie) {
        let token = cookie.slice('accessToken='.length);
        try { token = decodeURIComponent(token); } catch (_) {}
        if (token) headers.set('Authorization', `Bearer ${token}`);
      }
      // The first dispatch is synchronous, without a setTimeout(0) hop.
      fetch(url, {method: 'GET', cache: 'no-store', credentials: 'include',
                  headers, signal: controller.signal}).then(async response => {
        const data = response.ok ? await response.json() : null;
        state.result = {ok: response.ok, status: response.status, data,
          elapsedMs: performance.now() - started,
          visibility: document.visibilityState || 'unknown'};
      }).catch(() => { state.result = {ok: false, status: 0}; });
      return {state: 'started'};
    }"""

    def __init__(self, url: str, *, next_due: float = 0.0):
        self.url = url
        self.key = uuid.uuid4().hex
        self.next_due = next_due
        self.started_at = None
        self.failures = 0
        self.blocked = False

    def close(self, page) -> None:
        try:
            page.evaluate(self.STEP_SCRIPT, {"key": self.key, "url": self.url, "action": "cancel"})
        except Exception:
            pass
        self.started_at = None

    def step(self, page):
        now = time.monotonic()
        if self.blocked or (self.started_at is None and now < self.next_due):
            return None
        if self.started_at is not None and now - self.started_at >= self.TIMEOUT_SECONDS:
            self.close(page)
            self.next_due = now + 2.0
            return {"ok": False, "status": 0, "timedOut": True}
        try:
            state = page.evaluate(self.STEP_SCRIPT, {"key": self.key, "url": self.url, "action": "step"})
        except Exception:
            self.close(page)
            self.blocked = True
            return {"ok": False, "status": 0, "observerLost": True}
        if not isinstance(state, dict) or state.get("state") not in {"started", "running", "done"}:
            self.close(page)
            self.blocked = True
            return {"ok": False, "status": 0, "observerLost": True}
        if state["state"] in {"started", "running"}:
            if self.started_at is None:
                self.started_at = now
            return None
        result = state.get("result") or {}
        launched = self.started_at if self.started_at is not None else now
        self.started_at = None
        self.failures = 0 if result.get("ok") else min(self.failures + 1, 5)
        self.next_due = max(now + 0.05, launched + self.INTERVAL_SECONDS) if result.get("ok") else now + 2 ** self.failures
        if result.get("status") in {401, 403, 429}:
            # No further API hold or schedule request from this pass.
            self.blocked = True
        return result
