from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from engines.cgv_engine_watchdog import CgvEngine as WatchdogCgvEngine


class CgvEngine(WatchdogCgvEngine):
    """Final CGV engine with adaptive seat-modal synchronization.

    CGV can auto-select one neighbouring seat when a visitor clicks a seat while
    two or more visitor slots remain. The partner is not reliably "the next seat":
    depending on CGV's current grouping, B18 can select B17+B18 while B19 can
    select B19+B20.

    The API hold path already owns the exact N target seats. This layer mirrors
    that held set into CGV's React UI by observing the result of each click
    instead of predicting the partner direction. A click is kept only when every
    newly selected seat still belongs to the held target group.
    """

    # Local React observation only; these values do not change CGV API polling.
    # Most click updates land within one or two frames. Keep a bounded grace for
    # slower paints without the old 650 ms fixed pause after every click.
    PAIR_ACTION_SETTLE_SECONDS = 0.30
    PAIR_CLEAR_SETTLE_SECONDS = 0.35
    PAIR_MAX_BACKTRACKS = 4

    @staticmethod
    def _unique_ids(values) -> list[str]:
        return list(
            dict.fromkeys(
                str(value or "")
                for value in values
                if str(value or "")
            )
        )

    @classmethod
    def _extract_attach_pairs(
        cls, payload: Mapping[str, Any] | dict[str, Any]
    ) -> tuple[tuple[str, str], ...]:
        """Read only explicit two-seat ``attachGroupNo`` groups from CGV data.

        The current CGV page state exposes ``attachGroupNo`` on seat objects.
        Its exact semantics may vary by auditorium, so this metadata is only a
        candidate-order hint. Every click result is still verified against the
        real selected-seat state before it is accepted.
        """

        groups: dict[str, list[str]] = {}

        def walk(value: Any) -> None:
            if isinstance(value, Mapping):
                seat_id = str(value.get("seatLocNo", "") or "").strip()
                group_no = str(value.get("attachGroupNo", "") or "").strip()
                if seat_id and group_no:
                    groups.setdefault(group_no, []).append(seat_id)
                for child in value.values():
                    walk(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    walk(child)

        walk(payload)
        result: list[tuple[str, str]] = []
        for seat_ids in groups.values():
            unique = cls._unique_ids(seat_ids)
            # Be deliberately conservative. A group with a different cardinality
            # may represent another CGV seat product rather than the normal pair
            # behaviour we are trying to mirror.
            if len(unique) == 2:
                result.append((unique[0], unique[1]))
        return tuple(result)

    @classmethod
    def _missing_runs(
        cls,
        target_ids: list[str],
        selected_ids: list[str],
    ) -> list[list[str]]:
        """Return contiguous runs in the target order that are still missing."""

        target = cls._unique_ids(target_ids)
        selected = set(cls._unique_ids(selected_ids))
        runs: list[list[str]] = []
        current: list[str] = []
        for seat_id in target:
            if seat_id not in selected:
                current.append(seat_id)
            elif current:
                runs.append(current)
                current = []
        if current:
            runs.append(current)
        return runs

    @classmethod
    def _adaptive_anchor_candidates(
        cls,
        target_ids: list[str],
        selected_ids: list[str],
        rejected: set[str] | None = None,
        attach_pairs: tuple[tuple[str, str], ...] = (),
        learned_pairs: Mapping[str, tuple[str, ...]] | None = None,
    ) -> tuple[str, ...]:
        """Order candidate clicks without assuming left/right partner direction.

        Priority:
        1) an explicit CGV two-seat attach group fully inside the missing target;
        2) a previously observed anchor whose whole result stayed in target;
        3) for odd missing runs, an interior seat (both neighbours are targets);
        4) for even runs of four or more, interior anchors first so CGV cannot
           auto-attach a seat just outside the already-held block;
        5) remaining missing seats as a last observation-based fallback.
        """

        target = cls._unique_ids(target_ids)
        selected = set(cls._unique_ids(selected_ids))
        rejected = set(rejected or ())
        missing = [seat_id for seat_id in target if seat_id not in selected]
        missing_set = set(missing)
        if not missing:
            return ()
        if len(missing) == 1:
            return () if missing[0] in rejected else (missing[0],)

        ordered: list[str] = []

        def add(value: str) -> None:
            if value in missing_set and value not in rejected and value not in ordered:
                ordered.append(value)

        target_pos = {seat_id: index for index, seat_id in enumerate(target)}

        # Explicit CGV metadata is useful when present, but never trusted without
        # observing the click result.
        for pair in attach_pairs:
            pair_set = set(pair)
            if len(pair_set) == 2 and pair_set.issubset(missing_set):
                anchor = min(pair_set, key=lambda item: target_pos.get(item, 10**9))
                add(anchor)

        for anchor, observed in (learned_pairs or {}).items():
            observed_set = set(observed)
            if (
                anchor in missing_set
                and len(observed_set) >= 2
                and observed_set.issubset(missing_set)
            ):
                add(anchor)

        runs = cls._missing_runs(target, list(selected))

        # Odd runs: choose from the middle first. If CGV auto-pairs either left
        # or right, both outcomes remain inside the requested target block.
        odd_runs = [run for run in runs if len(run) >= 3 and len(run) % 2 == 1]
        odd_runs.sort(key=len, reverse=True)
        for run in odd_runs:
            center = (len(run) - 1) / 2
            for index in sorted(
                range(1, len(run) - 1),
                key=lambda idx: (abs(idx - center), idx),
            ):
                add(run[index])

        # For four or more held seats, the log-proven failure mode is an endpoint
        # click auto-attaching an outside neighbour (F21 -> F20+F21 and F24 ->
        # F24+F25). Probe the seats just inside each edge first: either neighbour
        # direction then remains inside the exact held block. A two-seat run has
        # no safe interior and keeps the observation-based endpoint order.
        even_runs = [run for run in runs if len(run) >= 2 and len(run) % 2 == 0]
        even_runs.sort(key=len, reverse=True)
        for run in even_runs:
            if len(run) >= 4:
                add(run[1])
                add(run[-2])
                for seat_id in run[2:-2]:
                    add(seat_id)
            add(run[0])
            add(run[-1])

        # Remaining odd/eccentric runs, including layouts split by an already
        # selected pair.
        for run in runs:
            if len(run) >= 3:
                for seat_id in run[1:-1]:
                    add(seat_id)
            if len(run) >= 2:
                add(run[0])
                add(run[-1])

        for seat_id in missing:
            add(seat_id)
        return tuple(ordered)

    @staticmethod
    def _dedupe_snapshot(snapshot: dict[str, Any], target_ids: list[str]) -> dict[str, Any]:
        """Normalize duplicate selected-seat DOM representations by seatLocNo."""

        if not isinstance(snapshot, dict):
            return {}
        target = list(
            dict.fromkeys(
                str(value or "")
                for value in target_ids
                if str(value or "")
            )
        )
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
        """Click one CGV seat and let the official UI decide its partner."""

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

    def _exact_seat_selection_snapshot(self, page, target_ids: list[str]) -> dict:
        raw = WatchdogCgvEngine._exact_seat_selection_snapshot(page, target_ids)
        return self._dedupe_snapshot(raw, target_ids)

    def _pairwise_wait(self, page) -> None:
        try:
            page.wait_for_timeout(self.API_UI_SYNC_INTERVAL_MS)
        except Exception:
            time.sleep(self.API_UI_SYNC_INTERVAL_MS / 1000.0)

    def _wait_for_selection_change(
        self,
        page,
        target_ids: list[str],
        before_ids: set[str],
        timeout_seconds: float,
        *,
        wanted_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Observe local React state until the selected-seat set changes."""

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        last: dict[str, Any] = {}
        while time.monotonic() < deadline and not self.stop_event.is_set():
            snapshot = self._exact_seat_selection_snapshot(page, target_ids)
            if snapshot:
                last = snapshot
                selected = set(self._unique_ids(snapshot.get("selectedIds", ())))
                if wanted_ids is not None:
                    if selected == wanted_ids:
                        return snapshot
                elif selected != before_ids or snapshot.get("ready"):
                    return snapshot
            self._pairwise_wait(page)
        return last

    def _clear_selected_state(
        self,
        page,
        target_ids: list[str],
        *,
        deadline: float,
    ) -> bool:
        """Clear CGV's current selected blocks with observed, bounded clicks."""

        for _ in range(max(2, len(target_ids) + 3)):
            if time.monotonic() >= deadline or self.stop_event.is_set():
                return False
            snapshot = self._exact_seat_selection_snapshot(page, target_ids)
            selected = self._unique_ids(snapshot.get("selectedIds", ())) if snapshot else []
            if not selected:
                return True

            before = set(selected)
            anchor = selected[0]
            if not self._click_pair_anchor(page, anchor):
                return False
            changed = self._wait_for_selection_change(
                page,
                target_ids,
                before,
                min(
                    self.PAIR_CLEAR_SETTLE_SECONDS,
                    max(0.0, deadline - time.monotonic()),
                ),
            )
            after = set(self._unique_ids(changed.get("selectedIds", ()))) if changed else before
            if after == before:
                return False
        snapshot = self._exact_seat_selection_snapshot(page, target_ids)
        return not self._unique_ids(snapshot.get("selectedIds", ())) if snapshot else False

    @staticmethod
    def _reject_equivalent_pair_anchors(
        rejected_by_state: dict[frozenset[str], set[str]],
        before_state: frozenset[str],
        anchor: str,
        added: set[str],
        attach_pairs: tuple[tuple[str, str], ...],
        learned_pairs: Mapping[str, tuple[str, ...]],
    ) -> None:
        rejected = rejected_by_state.setdefault(before_state, set())
        rejected.add(anchor)
        if len(added) != 2:
            return
        for candidate, observed in learned_pairs.items():
            if set(observed) == added:
                rejected.add(candidate)
        for pair in attach_pairs:
            if set(pair) == added:
                rejected.update(pair)

    def _select_api_seats_in_ui(self, page, payload, selected) -> bool:
        # The payload is already available from the official seat response that
        # won the hold race; extracting metadata is local work and adds no API
        # request or network delay.
        self._active_attach_pairs = self._extract_attach_pairs(
            payload if isinstance(payload, Mapping) else {}
        )
        try:
            return super()._select_api_seats_in_ui(page, payload, selected)
        finally:
            self._active_attach_pairs = ()

    def _normalize_active_seat_group(self, page, seat_ids: list[str]) -> bool:
        target_ids = self._unique_ids(seat_ids)
        if not target_ids:
            return False

        target_set = set(target_ids)
        attach_pairs = tuple(getattr(self, "_active_attach_pairs", ()) or ())
        rejected_by_state: dict[frozenset[str], set[str]] = {}
        learned_pairs: dict[str, tuple[str, ...]] = {}
        # (before_set, anchor, added_set) entries are enough for bounded
        # backtracking when an initially-valid pair later creates a dead end.
        history: list[tuple[frozenset[str], str, frozenset[str]]] = []
        observed_snapshot = False
        last_snapshot: dict[str, Any] = {}
        backtracks = 0
        click_count = 0
        rollback_count = 0

        budget_seconds = (
            self.API_UI_SYNC_ATTEMPTS * self.API_UI_SYNC_INTERVAL_MS / 1000.0
        )
        deadline = time.monotonic() + budget_seconds

        while time.monotonic() < deadline and not self.stop_event.is_set():
            snapshot = self._exact_seat_selection_snapshot(page, target_ids)
            if not snapshot:
                self._pairwise_wait(page)
                continue

            observed_snapshot = True
            last_snapshot = snapshot
            selected = self._unique_ids(snapshot.get("selectedIds", ()))
            selected_set = set(selected)

            if snapshot.get("ready"):
                if click_count or rollback_count:
                    self.log(
                        "[CGV] 적응형 좌석 동기화 완료 · "
                        f"{len(target_ids)}/{len(target_ids)}석 · "
                        f"선택 클릭 {click_count}회"
                        + (f" · 즉시 보정 {rollback_count}회" if rollback_count else ""),
                        "info",
                    )
                return True

            # Exact N seats are already mirrored. React may only be late enabling
            # 선택완료, so never touch a seat in this state.
            if selected_set == target_set and len(selected) == len(target_ids):
                self._pairwise_wait(page)
                continue

            # Any outside target seat is a stale or auto-attached partner. Clear
            # the UI state before continuing; the API hold remains untouched.
            if not selected_set.issubset(target_set):
                rollback_count += 1
                if not self._clear_selected_state(page, target_ids, deadline=deadline):
                    break
                history.clear()
                continue

            state_key = frozenset(selected_set)
            rejected = rejected_by_state.get(state_key, set())
            candidates = self._adaptive_anchor_candidates(
                target_ids,
                selected,
                rejected,
                attach_pairs=attach_pairs,
                learned_pairs=learned_pairs,
            )

            if not candidates:
                # The current accepted branch cannot finish. Backtrack by
                # rejecting the pair that led here, clear the UI, and rebuild.
                if history and backtracks < self.PAIR_MAX_BACKTRACKS:
                    before_state, branch_anchor, added = history[-1]
                    self._reject_equivalent_pair_anchors(
                        rejected_by_state,
                        before_state,
                        branch_anchor,
                        set(added),
                        attach_pairs,
                        learned_pairs,
                    )
                    backtracks += 1
                    rollback_count += 1
                    if not self._clear_selected_state(page, target_ids, deadline=deadline):
                        break
                    history.clear()
                    continue
                break

            anchor = candidates[0]
            before = set(selected_set)
            if not self._click_pair_anchor(page, anchor):
                rejected_by_state.setdefault(state_key, set()).add(anchor)
                self._pairwise_wait(page)
                continue

            click_count += 1
            changed = self._wait_for_selection_change(
                page,
                target_ids,
                before,
                min(
                    self.PAIR_ACTION_SETTLE_SECONDS,
                    max(0.0, deadline - time.monotonic()),
                ),
            )
            after = set(self._unique_ids(changed.get("selectedIds", ()))) if changed else before

            if after == before:
                # Do not double-click an anchor whose React update did not
                # materialize in the bounded observation window.
                rejected_by_state.setdefault(state_key, set()).add(anchor)
                continue

            # Keep any observed 1-seat or 2-seat change as long as the official
            # UI stayed entirely inside the exact held target. This deliberately
            # avoids assuming a fixed 2+2+... pattern.
            if before.issubset(after) and after.issubset(target_set):
                added = after - before
                if added:
                    learned_pairs[anchor] = tuple(
                        value for value in target_ids if value in added
                    )
                    history.append((state_key, anchor, frozenset(added)))
                    continue

            # Wrong-direction partner (for example B18 -> B17+B18): immediately
            # click the same anchor again and verify that CGV returned to the
            # exact pre-click state before trying another candidate.
            rejected_by_state.setdefault(state_key, set()).add(anchor)
            rollback_count += 1
            if after - target_set:
                self.log(
                    "[CGV] 좌석 자동묶음 방향 보정 · "
                    f"{anchor} 클릭이 목표 밖 좌석을 함께 선택해 즉시 되돌립니다.",
                    "info",
                )

            if self._click_pair_anchor(page, anchor):
                rolled = self._wait_for_selection_change(
                    page,
                    target_ids,
                    after,
                    min(
                        self.PAIR_CLEAR_SETTLE_SECONDS,
                        max(0.0, deadline - time.monotonic()),
                    ),
                    wanted_ids=before,
                )
                rolled_set = (
                    set(self._unique_ids(rolled.get("selectedIds", ())))
                    if rolled
                    else after
                )
                if rolled_set == before:
                    continue

            # Exact rollback failed: use the bounded clear routine rather than
            # stacking more speculative clicks on top of an unknown React state.
            if not self._clear_selected_state(page, target_ids, deadline=deadline):
                break
            history.clear()

        # Preserve legacy mocked-page compatibility when no DOM snapshot can be
        # observed; the submit helper still validates the actual button state.
        if not observed_snapshot:
            return True

        selected = self._unique_ids(last_snapshot.get("selectedIds", ()))
        self.log(
            "[CGV] 임시선점은 유지했지만 적응형 좌석 화면 동기화가 약 "
            f"{budget_seconds:.1f}초 내 완료되지 않았습니다 · "
            f"고유 선택 상태 {len(selected)}/{len(target_ids)}석",
            "warning",
        )
        return False
