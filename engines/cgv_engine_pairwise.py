from __future__ import annotations

import time
from typing import Any

from engines.cgv_engine_watchdog import CgvEngine as WatchdogCgvEngine


class CgvEngine(WatchdogCgvEngine):
    """Final CGV engine with pair-aware seat-modal synchronization.

    CGV's seat UI does not behave like a plain checkbox list when more than one
    visitor is selected.  While at least two visitor slots remain, clicking the
    front seat of a contiguous block selects that seat and the immediately next
    seat as one pair.  An odd final visitor is selected with one last click.

    The API hold path already holds the exact N target seats.  This layer only
    mirrors that already-held block into CGV's React UI without clicking every
    target seat individually (which could expand 2 seats into 4, 3 into 6,
    etc.).
    """

    # Give React enough time to apply the automatic partner selection before a
    # corrective action is considered.  The overall synchronization budget is
    # still inherited from the runtime layer (about three seconds).
    PAIR_ACTION_SETTLE_SECONDS = 0.65

    @staticmethod
    def _unique_ids(values) -> list[str]:
        return list(dict.fromkeys(str(value or "") for value in values if str(value or "")))

    @classmethod
    def _pairwise_click_plan(cls, target_ids: list[str]) -> tuple[str, ...]:
        """Return the front-seat anchors CGV needs for a contiguous N-seat block.

        Examples:
        1 -> [B10]
        2 -> [B10]                 (CGV adds B11)
        3 -> [B10, B12]            (B10+B11, then B12)
        5 -> [B10, B12, B14]       (2+2+1)
        8 -> [B10, B12, B14, B16]  (2+2+2+2)
        """

        target = cls._unique_ids(target_ids)
        return tuple(target[index] for index in range(0, len(target), 2))

    @classmethod
    def _pairwise_prefix_state(
        cls,
        target_ids: list[str],
        selected_ids: list[str],
    ) -> tuple[str, str | None, int | None]:
        """Classify the current React selection against CGV's 2+2+...(+1) rule."""

        target = cls._unique_ids(target_ids)
        selected = cls._unique_ids(selected_ids)
        count = len(selected)
        total = len(target)
        if not target:
            return "invalid", None, None

        # Stable intermediate states are 0, 2, 4, ... seats, plus the exact
        # final count for an odd-sized party.
        valid_counts = set(range(0, total, 2))
        valid_counts.add(total)
        if count not in valid_counts:
            return "invalid", None, None
        if set(selected) != set(target[:count]):
            return "invalid", None, None
        if count == total:
            return "complete", None, total

        remaining = total - count
        expected = count + (2 if remaining >= 2 else 1)
        return "advance", target[count], expected

    @staticmethod
    def _dedupe_snapshot(snapshot: dict[str, Any], target_ids: list[str]) -> dict[str, Any]:
        """Normalize duplicate selected-seat DOM representations.

        CGV can render the same selected seat both in the map and in its selected
        seat summary.  Counting DOM nodes instead of seatLocNo values produced
        misleading states such as 4/2 and 6/3 even when only N unique seats were
        selected.  Treat seatLocNo as the identity.
        """

        if not isinstance(snapshot, dict):
            return {}
        target = list(dict.fromkeys(str(value or "") for value in target_ids if str(value or "")))
        selected = list(
            dict.fromkeys(
                str(value or "")
                for value in snapshot.get("selectedIds", ()) or ()
                if str(value or "")
            )
        )
        target_set = set(target)
        selected_set = set(selected)
        normalized = dict(snapshot)
        normalized["selectedIds"] = selected
        normalized["extras"] = [value for value in selected if value not in target_set]
        normalized["missing"] = [value for value in target if value not in selected_set]
        normalized["ready"] = bool(
            not normalized["extras"]
            and not normalized["missing"]
            and len(selected) == len(target)
            and snapshot.get("submitReady")
        )
        return normalized

    @staticmethod
    def _click_pair_anchor(page, seat_id: str) -> bool:
        """Click one front-seat anchor and let CGV select its automatic partner."""

        try:
            result = page.evaluate(
                r"""
                seatId => {
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const nodes = Array.from(
                    document.querySelectorAll('button[data-seatlocno]')
                  );
                  const node = nodes.find(item =>
                    String(item.getAttribute('data-seatlocno') || '') === String(seatId) &&
                    visible(item)
                  );
                  if (!node || node.disabled || node.getAttribute('aria-disabled') === 'true') {
                    return false;
                  }
                  if (typeof node.scrollIntoView === 'function') {
                    node.scrollIntoView({block: 'center', inline: 'center'});
                  }
                  node.click();
                  return true;
                }
                """,
                str(seat_id),
            )
            return bool(result)
        except Exception:
            return False

    @staticmethod
    def _click_first_selected_seat(page) -> bool:
        """Remove one currently-selected CGV block when recovery is necessary."""

        try:
            result = page.evaluate(
                r"""
                () => {
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
                  const node = Array.from(
                    document.querySelectorAll('button[data-seatlocno]')
                  ).find(item => visible(item) && isSelected(item));
                  if (!node) return false;
                  node.click();
                  return true;
                }
                """
            )
            return bool(result)
        except Exception:
            return False

    def _exact_seat_selection_snapshot(self, page, target_ids: list[str]) -> dict:
        raw = WatchdogCgvEngine._exact_seat_selection_snapshot(page, target_ids)
        return self._dedupe_snapshot(raw, target_ids)

    def _normalize_active_seat_group(self, page, seat_ids: list[str]) -> bool:
        target_ids = self._unique_ids(seat_ids)
        if not target_ids:
            return False

        observed_snapshot = False
        last_snapshot: dict[str, Any] = {}
        reset_mode = False
        reset_pending: tuple[int, float] | None = None
        pair_pending: tuple[int, int, float, str] | None = None
        recovered_state = False

        for _attempt in range(self.API_UI_SYNC_ATTEMPTS):
            if self.stop_event.is_set():
                break

            snapshot = self._exact_seat_selection_snapshot(page, target_ids)
            now = time.monotonic()
            if snapshot:
                observed_snapshot = True
                last_snapshot = snapshot
                selected = self._unique_ids(snapshot.get("selectedIds", ()))
                selected_count = len(selected)

                if snapshot.get("ready"):
                    if recovered_state:
                        self.log(
                            "[CGV] CGV 2석 자동선택 규칙에 맞춰 좌석 화면 상태를 복구했습니다.",
                            "info",
                        )
                    return True

                # When a previous/buggy attempt left an impossible state, clear
                # the UI first. Clicking one selected anchor lets CGV remove the
                # associated pair; repeat only after the selected count falls.
                if reset_mode:
                    if selected_count == 0:
                        reset_mode = False
                        reset_pending = None
                        pair_pending = None
                        recovered_state = True
                    else:
                        if reset_pending is not None:
                            before_count, started = reset_pending
                            if selected_count < before_count:
                                reset_pending = None
                            elif now - started < self.PAIR_ACTION_SETTLE_SECONDS:
                                self._pairwise_wait(page)
                                continue
                            else:
                                reset_pending = None
                        if reset_pending is None:
                            if self._click_first_selected_seat(page):
                                reset_pending = (selected_count, now)
                        self._pairwise_wait(page)
                        continue

                # After an anchor click, never click its partner manually. Wait
                # for React to materialize the expected 2-seat (or final 1-seat)
                # prefix before issuing the next anchor.
                if pair_pending is not None:
                    from_count, expected_count, started, _anchor = pair_pending
                    expected_set = set(target_ids[:expected_count])
                    selected_set = set(selected)
                    if selected_count == expected_count and selected_set == expected_set:
                        pair_pending = None
                    elif (
                        selected_count > expected_count
                        or snapshot.get("extras")
                        or not selected_set.issubset(expected_set)
                    ):
                        reset_mode = True
                        pair_pending = None
                        recovered_state = True
                        self._pairwise_wait(page)
                        continue
                    elif now - started < self.PAIR_ACTION_SETTLE_SECONDS:
                        self._pairwise_wait(page)
                        continue
                    else:
                        # A half-applied pair is safer to reset than to click the
                        # same anchor again, which can toggle an already-selected
                        # pair off after a delayed React update.
                        reset_mode = True
                        pair_pending = None
                        recovered_state = True
                        self._pairwise_wait(page)
                        continue

                state, anchor, expected_count = self._pairwise_prefix_state(
                    target_ids,
                    selected,
                )
                if state == "complete":
                    # Exact N unique seats are selected; only wait for CGV to
                    # enable 선택완료. Do not touch any seat again.
                    self._pairwise_wait(page)
                    continue
                if state == "invalid":
                    reset_mode = True
                    recovered_state = True
                    self._pairwise_wait(page)
                    continue

                if anchor and expected_count is not None:
                    if self._click_pair_anchor(page, anchor):
                        pair_pending = (selected_count, expected_count, now, anchor)

            self._pairwise_wait(page)

        # Preserve legacy mocked-page compatibility when no DOM snapshot can be
        # observed; the submit helper still validates the actual button state.
        if not observed_snapshot:
            return True

        selected = self._unique_ids(last_snapshot.get("selectedIds", ()))
        self.log(
            "[CGV] 임시선점은 유지했지만 pair-aware 좌석 화면 동기화가 약 "
            f"{self.API_UI_SYNC_ATTEMPTS * self.API_UI_SYNC_INTERVAL_MS / 1000.0:.1f}초 내 "
            f"완료되지 않았습니다 · 고유 선택 상태 {len(selected)}/{len(target_ids)}석",
            "warning",
        )
        return False

    def _pairwise_wait(self, page) -> None:
        try:
            page.wait_for_timeout(self.API_UI_SYNC_INTERVAL_MS)
        except Exception:
            time.sleep(self.API_UI_SYNC_INTERVAL_MS / 1000.0)
