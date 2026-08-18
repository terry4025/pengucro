from __future__ import annotations

from typing import Any

from engines.cgv_engine_pairwise import CgvEngine as AdaptiveCgvEngine


class CgvEngine(AdaptiveCgvEngine):
    """Low-latency browser-observer layer for adaptive CGV seat synchronization.

    The adaptive layer decides *which* seat to probe and validates the resulting
    partner direction. This layer only shortens the local React hand-off: a
    MutationObserver is installed before any seat click, so Python does not need
    to poll the DOM every 25 ms waiting for the selected-seat set to change.

    No CGV API cadence, hold payload, authentication, or checkout behavior is
    changed here.
    """

    # These are hard fallback ceilings, not fixed sleeps. In the normal path the
    # MutationObserver resolves as soon as React mutates the seat selection.
    PAIR_ACTION_SETTLE_SECONDS = 0.22
    PAIR_CLEAR_SETTLE_SECONDS = 0.28
    PAIR_IDLE_FALLBACK_MS = 8
    _SEAT_OBSERVER_KEY = "__pengucroSeatSelectionObserverV2"

    @staticmethod
    def _install_selection_observer(page) -> bool:
        try:
            result = page.evaluate(
                r"""
                observerKey => {
                  const unique = values => Array.from(new Set(values.map(String)));
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const isSelected = node => {
                    const classes = String(node.className || '').toLowerCase();
                    const tokens = new Set(classes.split(/[\s_\-]+/));
                    return node.title === '선택됨' ||
                           node.getAttribute('aria-pressed') === 'true' ||
                           node.getAttribute('aria-selected') === 'true' ||
                           tokens.has('selected') || tokens.has('active') || tokens.has('on');
                  };
                  const clean = value => String(value || '').replace(/\s+/g, '');
                  const capture = () => {
                    const selectedIds = unique(
                      Array.from(document.querySelectorAll('button[data-seatlocno]'))
                        .filter(node => visible(node) && isSelected(node))
                        .map(node => node.getAttribute('data-seatlocno') || '')
                        .filter(Boolean)
                    );
                    const submit = Array.from(
                      document.querySelectorAll('button, a, div[role="button"]')
                    ).find(node => clean(node.textContent) === '선택완료' && visible(node));
                    const submitReady = Boolean(
                      submit && !submit.disabled &&
                      submit.getAttribute('aria-disabled') !== 'true'
                    );
                    return {
                      selectedIds,
                      submitReady,
                      selectedKey: JSON.stringify([...selectedIds].sort()),
                    };
                  };

                  const old = window[observerKey];
                  if (old && old.observer && typeof old.publish === 'function') {
                    old.publish();
                    return true;
                  }

                  const state = {
                    observer: null,
                    waiters: [],
                    snapshot: null,
                    version: 0,
                    publish: null,
                  };
                  state.publish = () => {
                    const next = capture();
                    const prev = state.snapshot;
                    const changed = !prev ||
                      next.selectedKey !== prev.selectedKey ||
                      next.submitReady !== prev.submitReady;
                    state.snapshot = next;
                    if (!changed) return;
                    state.version += 1;
                    const waiters = state.waiters.splice(0);
                    for (const waiter of waiters) {
                      try { waiter(next); } catch (_) {}
                    }
                  };
                  state.observer = new MutationObserver(() => state.publish());
                  state.observer.observe(document.documentElement || document.body, {
                    subtree: true,
                    childList: true,
                    attributes: true,
                    attributeFilter: [
                      'class', 'title', 'aria-pressed', 'aria-selected',
                      'aria-disabled', 'disabled', 'style'
                    ],
                  });
                  state.publish();
                  window[observerKey] = state;
                  return true;
                }
                """,
                CgvEngine._SEAT_OBSERVER_KEY,
            )
            return bool(result)
        except Exception:
            return False

    @staticmethod
    def _teardown_selection_observer(page) -> None:
        try:
            page.evaluate(
                r"""
                observerKey => {
                  const state = window[observerKey];
                  if (!state) return;
                  try {
                    if (state.observer) state.observer.disconnect();
                  } catch (_) {}
                  const waiters = Array.isArray(state.waiters)
                    ? state.waiters.splice(0) : [];
                  for (const waiter of waiters) {
                    try { waiter(state.snapshot || null); } catch (_) {}
                  }
                  delete window[observerKey];
                }
                """,
                CgvEngine._SEAT_OBSERVER_KEY,
            )
        except Exception:
            pass

    def _pairwise_wait(self, page) -> None:
        # This remains only as a fallback for states where no seat mutation is
        # expected (for example exact N seats selected while 선택완료 is enabling).
        try:
            page.wait_for_timeout(self.PAIR_IDLE_FALLBACK_MS)
        except Exception:
            if self.stop_event.wait(self.PAIR_IDLE_FALLBACK_MS / 1000.0):
                return

    def _wait_for_selection_change(
        self,
        page,
        target_ids: list[str],
        before_ids: set[str],
        timeout_seconds: float,
        *,
        wanted_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Wait inside the browser for React's actual seat-selection mutation.

        The observer is installed before the click by ``_normalize_active_seat_group``.
        If React already changed synchronously, the cached observer snapshot makes
        this call resolve immediately. Otherwise a waiter is notified by the next
        relevant DOM mutation. The old 25 ms Python polling path is retained only
        as a compatibility fallback when observer installation/evaluation fails.
        """

        timeout_ms = max(1, int(max(0.0, timeout_seconds) * 1000))
        try:
            result = page.evaluate(
                r"""
                async ({observerKey, beforeIds, wantedIds, timeoutMs}) => {
                  const state = window[observerKey];
                  if (!state || typeof state.publish !== 'function') return null;

                  const key = values => JSON.stringify(
                    Array.from(new Set((values || []).map(String))).sort()
                  );
                  const beforeKey = key(beforeIds);
                  const hasWanted = Array.isArray(wantedIds);
                  const wantedKey = hasWanted ? key(wantedIds) : '';

                  const matches = snapshot => {
                    if (!snapshot) return false;
                    const selectedKey = snapshot.selectedKey || key(snapshot.selectedIds);
                    if (hasWanted) return selectedKey === wantedKey;
                    return selectedKey !== beforeKey || Boolean(snapshot.submitReady);
                  };

                  state.publish();
                  if (matches(state.snapshot)) {
                    return {...state.snapshot, observerImmediate: true};
                  }

                  return await new Promise(resolve => {
                    let settled = false;
                    let timer = null;
                    const finish = snapshot => {
                      if (settled || !matches(snapshot)) return;
                      settled = true;
                      if (timer) clearTimeout(timer);
                      resolve({...snapshot, observerImmediate: false});
                    };
                    const waiter = snapshot => finish(snapshot);
                    state.waiters.push(waiter);

                    // Close the tiny race between the immediate check and waiter
                    // registration. publish() is local DOM inspection only.
                    state.publish();
                    if (matches(state.snapshot)) {
                      const index = state.waiters.indexOf(waiter);
                      if (index >= 0) state.waiters.splice(index, 1);
                      finish(state.snapshot);
                      return;
                    }

                    timer = setTimeout(() => {
                      if (settled) return;
                      settled = true;
                      const index = state.waiters.indexOf(waiter);
                      if (index >= 0) state.waiters.splice(index, 1);
                      resolve(state.snapshot || null);
                    }, timeoutMs);
                  });
                }
                """,
                {
                    "observerKey": self._SEAT_OBSERVER_KEY,
                    "beforeIds": sorted(str(value) for value in before_ids),
                    "wantedIds": (
                        sorted(str(value) for value in wanted_ids)
                        if wanted_ids is not None
                        else None
                    ),
                    "timeoutMs": timeout_ms,
                },
            )
            if isinstance(result, dict):
                return self._dedupe_snapshot(result, target_ids)
        except Exception:
            pass

        return super()._wait_for_selection_change(
            page,
            target_ids,
            before_ids,
            timeout_seconds,
            wanted_ids=wanted_ids,
        )

    def _normalize_active_seat_group(self, page, seat_ids: list[str]) -> bool:
        installed = self._install_selection_observer(page)
        try:
            return super()._normalize_active_seat_group(page, seat_ids)
        finally:
            if installed:
                self._teardown_selection_observer(page)
