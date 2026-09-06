from __future__ import annotations

import time
from typing import Any, Iterable

from engines.cgv_client import (
    CgvSeat,
    CgvSeatGroup,
    rank_recommended_seat_groups,
    normalize_seat_name,
    normalize_time,
    parse_api_seats,
    parse_seat_groups,
    recommend_cgv_seats,
    select_schedule,
)
from engines.cgv_engine_visitor_dom_runtime import CgvEngine as VisitorDomCgvEngine


class CgvEngine(VisitorDomCgvEngine):
    """Final CGV runtime that combines time and seat priorities.

    A preferred screening is not considered a winner merely because the
    screening exists.  Each preferred time is checked in user order and only
    becomes active when at least one configured seat strategy is available.
    If time #1 has none of the requested seats, time #2 is checked immediately,
    then #3, and so on.  When a full pass has no winner the same ordered ladder
    is repeated instead of pinning cancellation monitoring to time #1.

    Seat reads remain one-at-a-time and obey the guarded engine's 350 ms floor.
    The first official seat response opened by CGV is reused without a duplicate
    request.  Once a winner is found, the mature direct price/hold, adaptive
    pair UI synchronization, checkout and rate-limit fallback paths take over.
    """

    PRIORITY_SEAT_REQUEST_INTERVAL_SECONDS = 0.350
    PRIORITY_SCHEDULE_REFRESH_SECONDS = 2.5
    PRIORITY_AUTO_ATTEMPTS_PER_TIME = 3
    PRIORITY_READ_TIMEOUT_MS = 2000
    PRIORITY_FAILURE_RETRY_SECONDS = 30.0
    PRIORITY_TIME_BUDGET_SECONDS = 3.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._priority_rotation_mode = "strict"
        self._priority_rank_cache = {}
        self._priority_failures = {}
        self._priority_movie = ""
        self._priority_auditorium = ""
        self._priority_format = ""
        self._priority_preferred_times: list[str] = []
        self._priority_manual_groups: tuple[CgvSeatGroup, ...] = ()
        self._priority_auto_mode = ""
        self._priority_auto_label = ""
        self._priority_original_cgv: dict[str, Any] = {}
        self._priority_schedule_payload: dict[str, Any] = {}
        self._priority_schedule_url = ""
        self._priority_last_schedule_refresh = 0.0
        self._priority_last_seat_read = 0.0
        self._priority_primary_key: tuple[str, ...] = ()
        self._priority_initial_consumed = False
        self._priority_active_schedule: dict[str, Any] | None = None
        self._priority_active_groups: tuple[CgvSeatGroup, ...] = ()
        self._priority_claim_returns_on_conflict = False
        self._priority_page_schedule_key: tuple[str, ...] = ()

    @staticmethod
    def _schedule_key(schedule: dict[str, Any] | None) -> tuple[str, ...]:
        item = schedule or {}
        return tuple(
            str(item.get(key, "") or "")
            for key in ("siteNo", "scnYmd", "scnsNo", "scnSseq")
        )

    @staticmethod
    def _priority_time_label(schedule: dict[str, Any]) -> str:
        raw = normalize_time(schedule.get("scnsrtTm"))
        return f"{raw[:2]}:{raw[2:]}" if len(raw) == 4 else raw

    def _fast_monitor_conflict_limit(self) -> int:
        if self._priority_claim_returns_on_conflict:
            return 1
        return super()._fast_monitor_conflict_limit()

    @staticmethod
    def _synthetic_engine_group(people: int) -> list[str]:
        count = max(1, min(int(people), 8))
        return [f"A{index}" for index in range(1, count + 1)]

    def make_reservation_thread(self, reservation_data: dict[str, Any]) -> None:
        data = dict(reservation_data or {})
        metadata = dict(data.get("engine_metadata", {}) or {})
        cgv = dict(metadata.get("cgv", {}) or {})
        people = max(1, min(int(data.get("people", 1) or 1), 8))

        original_seats = str(cgv.get("seats", "") or "").strip()
        manual_groups = parse_seat_groups(original_seats, people)
        raw_structured = cgv.get("seat_groups")
        if isinstance(raw_structured, (list, tuple)):
            structured_text = " | ".join(
                ",".join(str(seat or "").strip() for seat in group if str(seat or "").strip())
                for group in raw_structured
                if isinstance(group, (list, tuple))
            )
            structured_groups = parse_seat_groups(structured_text, people)
            if structured_groups:
                manual_groups = structured_groups

        auto_mode = str(cgv.get("auto_seat_mode", "") or "").strip()
        auto_label = str(cgv.get("auto_seat_label", "") or "").strip()
        show_time = str(data.get("reservationTime", "") or "").strip()
        preferred = list(cgv.get("preferred_times") or ([show_time] if show_time else []))

        self._priority_movie = str(cgv.get("movie") or data.get("themePK", "") or "").strip()
        self._priority_auditorium = str(cgv.get("auditorium", "") or "").strip()
        self._priority_format = str(cgv.get("format", "") or "").strip()
        self._priority_preferred_times = [str(value) for value in preferred if str(value).strip()]
        self._priority_manual_groups = tuple(manual_groups)
        self._priority_auto_mode = auto_mode
        self._priority_auto_label = auto_label
        self._priority_original_cgv = dict(cgv)
        self._priority_rotation_mode = "fast" if cgv.get("priority_rotation_mode") == "fast" else "strict"
        self._priority_rank_cache.clear()
        self._priority_failures.clear()
        self._priority_schedule_payload = {}
        self._priority_schedule_url = ""
        self._priority_last_schedule_refresh = 0.0
        self._priority_last_seat_read = 0.0
        self._priority_primary_key = ()
        self._priority_initial_consumed = False
        self._priority_active_schedule = None
        self._priority_active_groups = tuple(manual_groups)
        self._priority_claim_returns_on_conflict = False
        self._priority_page_schedule_key = ()

        # Base CGV still validates that at least one concrete group exists before
        # it reaches our real-seat resolver. Auto-only bookings therefore receive
        # a private placeholder in the copied request. It is never used by this
        # class and never replaces _priority_manual_groups.
        if auto_mode and not manual_groups:
            placeholder = self._synthetic_engine_group(people)
            cgv["seats"] = ",".join(placeholder)
            cgv["seat_groups"] = [placeholder]
            metadata["cgv"] = cgv
            data["engine_metadata"] = metadata

        try:
            return super().make_reservation_thread(data)
        finally:
            self._priority_active_schedule = None
            self._priority_active_groups = ()
            self._priority_claim_returns_on_conflict = False
            self._priority_page_schedule_key = ()

    def _race_schedule(self, page, url: str, concurrency: int) -> dict[str, Any]:
        result = super()._race_schedule(page, url, concurrency)
        self._priority_schedule_url = str(url or "")
        if result.get("ok") and isinstance(result.get("data"), dict):
            self._priority_schedule_payload = dict(result["data"])
            self._priority_last_schedule_refresh = time.monotonic()
        return result

    def _ordered_schedule_candidates(
        self, primary: dict[str, Any]
    ) -> list[dict[str, Any]]:
        payload = self._priority_schedule_payload
        if not payload or not self._priority_preferred_times:
            return [primary]

        result: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for preferred in self._priority_preferred_times:
            item = select_schedule(
                payload,
                movie=self._priority_movie,
                auditorium=self._priority_auditorium,
                format_name=self._priority_format,
                preferred_times=[preferred],
            )
            if not item:
                continue
            key = self._schedule_key(item)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)

        primary_key = self._schedule_key(primary)
        if primary_key and primary_key not in seen:
            # A successful current payload containing another valid screening is
            # authoritative enough to put that live identity ahead of a primary
            # row that has disappeared. Keep the old row only as a last-resort
            # fallback for genuinely partial publication responses.
            result.append(primary)
        return result or [primary]

    def _refresh_priority_schedule_payload(self, page) -> None:
        if not self._priority_schedule_url:
            return
        now = time.monotonic()
        if now - self._priority_last_schedule_refresh < self.PRIORITY_SCHEDULE_REFRESH_SECONDS:
            return
        try:
            result = super()._race_schedule(page, self._priority_schedule_url, 1)
        except Exception:
            self._priority_last_schedule_refresh = now
            return
        self._priority_last_schedule_refresh = now
        if result.get("ok") and isinstance(result.get("data"), dict):
            self._priority_schedule_payload = dict(result["data"])

    def _wait_for_priority_seat_read_slot(self) -> bool:
        if self._priority_last_seat_read <= 0:
            return not self.stop_event.is_set()
        remaining = (
            self.PRIORITY_SEAT_REQUEST_INTERVAL_SECONDS
            - (time.monotonic() - self._priority_last_seat_read)
        )
        if remaining > 0 and self.stop_event.wait(remaining):
            return False
        return not self.stop_event.is_set()

    def _fetch_priority_seat_payload(
        self, page, schedule: dict[str, Any]
    ) -> dict[str, Any]:
        if not self._wait_for_priority_seat_read_slot():
            return {"ok": False, "status": 0, "stopped": True}
        auth = self._browser_auth_data(page)
        url = self._seat_url(schedule, auth.get("custNo", ""))
        try:
            result = page.evaluate(
                r"""
                async ({url, timeoutMs}) => {
                  const controller = new AbortController();
                  const timeout = setTimeout(() => controller.abort(), timeoutMs);
                  try {
                    const headers = new Headers({
                      'Accept': 'application/json, text/plain, */*',
                      'Accept-Language': 'ko-KR',
                    });
                    const item = String(document.cookie || '').split('; ')
                      .find(value => value.startsWith('accessToken='));
                    if (item) {
                      let token = item.slice('accessToken='.length);
                      try { token = decodeURIComponent(token); } catch (_) {}
                      if (token) headers.set('Authorization', `Bearer ${token}`);
                    }
                    const response = await fetch(url, {
                      method: 'GET',
                      cache: 'no-store',
                      credentials: 'include',
                      signal: controller.signal,
                      headers,
                    });
                    let data = null;
                    try { data = await response.json(); } catch (_) {}
                    return {
                      ok: response.ok,
                      status: response.status,
                      data,
                      retryAfter: response.headers.get('retry-after') || '',
                    };
                  } catch (error) {
                    return {ok: false, status: 0, error: String(error || 'fetch failed')};
                  } finally {
                    clearTimeout(timeout);
                  }
                }
                """,
                {"url": url, "timeoutMs": self.PRIORITY_READ_TIMEOUT_MS},
            )
        except Exception as exc:
            result = {"ok": False, "status": 0, "error": str(exc)}
        finally:
            self._priority_last_seat_read = time.monotonic()

        return dict(result) if isinstance(result, dict) else {"ok": False, "status": 0}

    @staticmethod
    def _manual_available_group(
        seats: Iterable[CgvSeat], groups: tuple[CgvSeatGroup, ...]
    ) -> CgvSeatGroup | None:
        available = {
            normalize_seat_name(seat.label)
            for seat in seats
            if bool(seat.available)
        }
        for group in groups:
            if all(normalize_seat_name(label) in available for label in group.seats):
                return group
        return None

    def _auto_available_group(
        self,
        seats: tuple[CgvSeat, ...],
        schedule: dict[str, Any],
        people: int,
        *,
        excluded: Iterable[Iterable[str]] = (),
    ) -> CgvSeatGroup | None:
        mode = self._priority_auto_mode
        if not mode or not seats:
            return None
        # Cache geometry/rank only: a sale must not rebuild the seating layout.
        layout = tuple((seat.label, seat.row, seat.number, seat.left_passage, seat.right_passage)
                       for seat in seats)
        site = str(schedule.get("siteNo", "") or "")
        auditorium = str(schedule.get("expoScnsNm") or schedule.get("scnsNm") or "")
        format_name = str(schedule.get("movkndDsplEnm") or schedule.get("movkndDsplNm") or "")
        excluded_groups = {frozenset(normalize_seat_name(s) for s in group) for group in excluded}
        available = {normalize_seat_name(seat.label) for seat in seats if seat.available}
        for candidate_mode in ((mode, "best") if mode != "best" else (mode,)):
            key = (site, auditorium, format_name, people, candidate_mode, layout)
            ranked = self._priority_rank_cache.get(key)
            if ranked is None:
                recommendations = recommend_cgv_seats(
                    seats, site_no=site, auditorium=auditorium, format_name=format_name)
                ranked = rank_recommended_seat_groups(
                    seats, recommendations, people, mode=candidate_mode, include_fallback=False)
                if len(self._priority_rank_cache) >= 8:
                    self._priority_rank_cache.pop(next(iter(self._priority_rank_cache)))
                self._priority_rank_cache[key] = ranked
            for group in ranked:
                labels = frozenset(normalize_seat_name(label) for label in group)
                if labels not in excluded_groups and labels <= available:
                    return CgvSeatGroup(group)
        return None

    def _priority_failed_groups(self, key, payload):
        """Retry a failed group only after its own reopening or a bounded TTL."""
        available = {normalize_seat_name(s.label) for s in parse_api_seats(payload) if s.available}
        records = self._priority_failures.setdefault(key, {})
        now = time.monotonic()
        for labels, (failed_at, seen_unavailable) in list(records.items()):
            group_available = set(labels) <= available
            if now - failed_at >= self.PRIORITY_FAILURE_RETRY_SECONDS or (seen_unavailable and group_available):
                del records[labels]
            elif not group_available:
                records[labels] = (failed_at, True)
        return set(records)

    def _priority_should_rotate(self, started, attempts, auto_attempts, excluded):
        if self._priority_rotation_mode == "fast" and attempts:
            if time.monotonic() - started >= self.PRIORITY_TIME_BUDGET_SECONDS:
                return True
        if auto_attempts < self.PRIORITY_AUTO_ATTEMPTS_PER_TIME:
            return False
        # Do not fetch the old screening simply to discover the auto quota is
        # exhausted. Untried manual priorities must still get their turn.
        return not any(tuple(normalize_seat_name(s) for s in group.seats) not in excluded
                       for group in self._priority_manual_groups)

    def _choose_priority_group(
        self,
        payload: dict[str, Any],
        schedule: dict[str, Any],
        people: int,
        *,
        excluded: Iterable[Iterable[str]] = (),
    ) -> CgvSeatGroup | None:
        seats = parse_api_seats(payload)
        if not seats:
            return None
        excluded_groups = {
            tuple(normalize_seat_name(label) for label in group)
            for group in excluded
        }
        manual_groups = tuple(
            group
            for group in self._priority_manual_groups
            if tuple(normalize_seat_name(label) for label in group.seats)
            not in excluded_groups
        )
        manual = self._manual_available_group(seats, manual_groups)
        if manual is not None:
            return manual
        return self._auto_available_group(
            seats,
            schedule,
            people,
            excluded=excluded_groups,
        )

    def _read_schedule_once(
        self,
        page,
        schedule: dict[str, Any],
        people: int,
        *,
        allow_initial: bool,
    ) -> tuple[CgvSeatGroup | None, dict[str, Any] | None, int]:
        if allow_initial and not self._priority_initial_consumed:
            captured = self._consume_initial_seat_response(schedule)
            self._priority_initial_consumed = True
            if isinstance(captured, dict) and isinstance(captured.get("data"), dict):
                self._priority_last_seat_read = time.monotonic()
                payload = dict(captured["data"])
                group = self._choose_priority_group(payload, schedule, people)
                return group, payload, int(captured.get("status", 200) or 200)

        result = self._fetch_priority_seat_payload(page, schedule)
        status = int(result.get("status", 0) or 0)
        payload = result.get("data")
        if not result.get("ok") or not isinstance(payload, dict):
            return None, None, status
        api_status = int(payload.get("statusCode", 0) or 0)
        if api_status in {-1001, -1002}:
            return None, payload, 401
        if api_status != 0:
            return None, payload, status
        group = self._choose_priority_group(payload, schedule, people)
        return group, payload, status

    def _seed_initial_payload(
        self, schedule: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        auth = self._browser_auth_data(getattr(self, "_priority_page", None)) if False else {}
        del auth  # document that the seed deliberately needs no extra headers
        self._initial_seat_response = {
            "url": self._seat_url(schedule, ""),
            "status": 200,
            "data": payload,
            "requestHeaders": {},
        }

    def _activate_priority_schedule(
        self, page, schedule: dict[str, Any], people: int
    ) -> bool:
        self._restore_fetch(page)
        if not self._enter_visitor_page(page, schedule):
            return False
        handler = self._begin_initial_seat_response_capture(page)
        try:
            return bool(self._select_visitors(page, people))
        finally:
            self._end_initial_seat_response_capture(page, handler)

    def _prepare_api_hold_ui(
        self,
        page,
        schedule: dict[str, Any],
        people: int,
    ) -> bool:
        candidate_key = self._schedule_key(schedule)
        if (
            self._priority_claim_returns_on_conflict
            and candidate_key
            and candidate_key != self._priority_page_schedule_key
        ):
            if not self._activate_priority_schedule(page, schedule, people):
                return False
            self._priority_page_schedule_key = candidate_key
            self._api_hold_ui_schedule_key = candidate_key
        return super()._prepare_api_hold_ui(page, schedule, people)

    def _watch_and_hold_api(
        self,
        page,
        schedule: dict[str, Any],
        groups: tuple[CgvSeatGroup, ...],
        people: int,
        developer_mode: bool,
        cgv: dict[str, Any],
    ) -> tuple[bool, bool]:
        self._priority_primary_key = self._schedule_key(schedule)
        self._priority_active_schedule = schedule
        self._priority_active_groups = self._priority_manual_groups
        self._priority_page_schedule_key = self._schedule_key(schedule)

        # A single explicit time with only manual groups already has exactly the
        # desired semantics in the mature monitor. Avoid any extra preflight.
        if (len(self._priority_preferred_times) <= 1 and not self._priority_auto_mode
                and not getattr(self, "_priority_preopen_monitor", False)):
            return super()._watch_and_hold_api(
                page, schedule, groups, people, developer_mode, cgv
            )

        pass_no = 0
        self._priority_failures.clear()
        previous_conflict_at = None
        while not self.stop_event.is_set():
            self._refresh_priority_schedule_payload(page)
            if getattr(self, "_priority_schedule_blocked", False):
                return False, False
            candidates = self._ordered_schedule_candidates(schedule)
            if not candidates:
                self.stop_event.wait(self.PRIORITY_SEAT_REQUEST_INTERVAL_SECONDS)
                continue

            visited = set()
            index = 0
            while candidates:
                candidate = candidates.pop(0)
                index += 1
                visited.add(self._schedule_key(candidate))
                if self.stop_event.is_set():
                    return False, False
                candidate_key = self._schedule_key(candidate)
                allow_initial = (
                    pass_no == 0 and candidate_key == self._priority_primary_key
                )
                excluded_groups: set[tuple[str, ...]] = set()
                candidate_payload: dict[str, Any] | None = None
                first_candidate_read = True
                auto_attempts = 0
                candidate_started = time.monotonic()
                candidate_attempts = 0
                selection_started = candidate_started
                manual_labels = {tuple(normalize_seat_name(s) for s in g.seats)
                                 for g in self._priority_manual_groups}

                while not self.stop_event.is_set():
                    self._refresh_priority_schedule_payload(page)
                    if getattr(self, "_priority_schedule_blocked", False):
                        return False, False
                    if candidate_key not in {self._schedule_key(item)
                                             for item in self._ordered_schedule_candidates(schedule)}:
                        break
                    if first_candidate_read:
                        group, payload, status = self._read_schedule_once(
                            page,
                            candidate,
                            people,
                            allow_initial=allow_initial,
                        )
                        first_candidate_read = False
                        candidate_payload = payload
                        if isinstance(payload, dict):
                            excluded_groups.update(self._priority_failed_groups(candidate_key, payload))
                            if excluded_groups:
                                group = self._choose_priority_group(payload, candidate, people,
                                                                    excluded=excluded_groups)
                    else:
                        payload = candidate_payload
                        status = 200
                        group = (
                            self._choose_priority_group(
                                payload,
                                candidate,
                                people,
                                excluded=excluded_groups,
                            )
                            if isinstance(payload, dict)
                            else None
                        )

                    if status in {401, 403, 429}:
                        self._last_fast_monitor_exit_reason = {
                            401: "unauthorized",
                            403: "access-forbidden",
                            429: "rate-limited",
                        }[status]
                        self.log(
                            f"[CGV] 시간 우선순위 좌석 확인 중 HTTP {status} 감지 · "
                            "추가 시간대 조회를 중단하고 기존 안전 정책으로 전환합니다.",
                            "warning",
                        )
                        # Auto-only mode has no concrete browser fallback group;
                        # never let its validation placeholder become a real seat.
                        if not self._priority_manual_groups:
                            return False, False
                        self._priority_active_groups = self._priority_manual_groups
                        return False, True

                    if group is None or payload is None:
                        self.silent_tick(
                            f"CGV 시간 {index}순위 {self._priority_time_label(candidate)} · "
                            "좌석 우선순위 일치 좌석 없음 · 다음 시간 확인"
                        )
                        break

                    group_labels = tuple(normalize_seat_name(s) for s in group.seats)
                    is_auto = group_labels not in manual_labels
                    if is_auto and auto_attempts >= self.PRIORITY_AUTO_ATTEMPTS_PER_TIME:
                        self.log(
                            f"[CGV] 시간 {index}순위 자동 추천 {auto_attempts}개 시도 완료 · "
                            "다음 희망 시간 확인 후 남은 추천 순서로 재개합니다.", "info")
                        break
                    if self._priority_should_rotate(candidate_started, candidate_attempts, auto_attempts, excluded_groups):
                        self.log(f"[CGV] 시간 {index}순위 탐색 시간 사용 · 다음 시간 확인 후 남은 좌석 재개", "info")
                        break
                    if is_auto:
                        auto_attempts += 1
                    candidate_attempts += 1

                    self._priority_active_schedule = candidate
                    self._priority_active_groups = (group,)
                    self.log(
                        f"[CGV] 시간 {index}순위 {self._priority_time_label(candidate)} · "
                        f"좌석 우선순위 확보 가능 {', '.join(group.seats)} · 고속 선점 진행",
                        "success",
                    )

                    # The preflight seat response is a one-shot first monitor
                    # seed for every candidate.  For a fallback screening this
                    # lets price/hold finish before the slower route and visitor
                    # UI transition; the UI is synchronized only after the hold.
                    selected_at = time.monotonic()
                    opened_at = getattr(self, "_cgv_open_detected_at", None)
                    open_detail = f" · 회차 감지 후 {(selected_at-opened_at)*1000:.0f}ms" if opened_at is not None else ""
                    switch_detail = f" · 직전 경합 후 {(selected_at-previous_conflict_at)*1000:.0f}ms" if previous_conflict_at is not None else ""
                    candidate_timing_log = (f"[CGV 속도] 후보 확정 · {self._priority_time_label(candidate)} · "
                                            f"조회·선택 {(selected_at-selection_started)*1000:.0f}ms{open_detail}{switch_detail}")
                    self._seed_initial_payload(candidate, payload)
                    clean_cgv = dict(self._priority_original_cgv or cgv)
                    self._priority_claim_returns_on_conflict = True
                    self._last_fast_monitor_exit_reason = ""
                    try:
                        held, use_fallback = super()._watch_and_hold_api(
                            page,
                            candidate,
                            (group,),
                            people,
                            developer_mode,
                            clean_cgv,
                        )
                    finally:
                        self._priority_claim_returns_on_conflict = False

                    self.log(candidate_timing_log, "info")
                    if held or use_fallback or self.stop_event.is_set():
                        return held, use_fallback
                    if self._last_fast_monitor_exit_reason != "seat-conflict":
                        return held, use_fallback

                    excluded_groups.add(group_labels)
                    previous_conflict_at = time.monotonic()
                    if is_auto or self._priority_rotation_mode == "fast":
                        self._priority_failures.setdefault(candidate_key, {})[group_labels] = (previous_conflict_at, False)
                    if self._priority_should_rotate(candidate_started, candidate_attempts, auto_attempts, excluded_groups):
                        self.log(f"[CGV] 시간 {index}순위 탐색 한도 · 추가 좌석 조회 없이 다음 희망 시간 확인", "info")
                        break
                    self.silent_tick(
                        f"CGV 시간 {index}순위 {self._priority_time_label(candidate)} · "
                        f"{', '.join(group.seats)} 선점 경합 · 다음 좌석 우선순위 확인"
                    )
                    # A failed hold invalidates the preflight availability.
                    # Never seed another candidate from that same stale map.
                    selection_started = time.monotonic()
                    refreshed = self._fetch_priority_seat_payload(page, candidate)
                    status = int(refreshed.get("status", 0) or 0)
                    candidate_payload = refreshed.get("data") if refreshed.get("ok") else None
                    if status in {401, 403, 429}:
                        self._last_fast_monitor_exit_reason = {
                            401: "unauthorized", 403: "access-forbidden", 429: "rate-limited"
                        }[status]
                        self.log(f"[CGV] 경합 후 최신 좌석 조회 HTTP {status} · "
                                 "다른 시간대의 추가 API 선점은 중단합니다.", "warning")
                        return False, bool(self._priority_manual_groups)
                    if not isinstance(candidate_payload, dict):
                        break
                    if int(candidate_payload.get("statusCode", 0) or 0) != 0:
                        self._last_fast_monitor_exit_reason = "priority-refresh-api-error"
                        self.log("[CGV] 경합 후 최신 좌석 API 오류 · 오래된 좌석표를 재사용하지 않습니다.",
                                 "warning")
                        return False, bool(self._priority_manual_groups)
                    self._priority_failed_groups(candidate_key, candidate_payload)

                # Fold sequential publication into this pass before revisiting
                # time #1. Preserve user ordering among all unvisited rows.
                self._refresh_priority_schedule_payload(page)
                if getattr(self, "_priority_schedule_blocked", False):
                    return False, False
                candidates = [item for item in self._ordered_schedule_candidates(schedule)
                              if self._schedule_key(item) not in visited]

            pass_no += 1
            order = " → ".join(
                self._priority_time_label(item) for item in self._ordered_schedule_candidates(schedule)
            )
            self.silent_tick(
                f"CGV 시간 우선순위 순환 감시 · {order} · 원하는 좌석 대기"
            )

        return False, False

    def _prepare_browser_fallback_page(
        self,
        page,
        *,
        schedule: dict[str, Any] | None = None,
        people: int = 1,
        fallback_reason: str = "",
    ):
        active = self._priority_active_schedule or schedule
        return super()._prepare_browser_fallback_page(
            page,
            schedule=active,
            people=people,
            fallback_reason=fallback_reason,
        )

    def _select_and_hold_seats(
        self,
        page,
        groups: tuple[CgvSeatGroup, ...],
        people: int,
        developer_mode: bool,
        schedule: dict[str, Any] | None = None,
        fallback_reason: str = "",
    ) -> bool:
        active_groups = self._priority_active_groups or groups
        active_schedule = self._priority_active_schedule or schedule
        # Never expose an auto-only validation placeholder to the browser path.
        if self._priority_auto_mode and not self._priority_manual_groups and not self._priority_active_groups:
            return False
        return super()._select_and_hold_seats(
            page,
            active_groups,
            people,
            developer_mode,
            schedule=active_schedule,
            fallback_reason=fallback_reason,
        )
