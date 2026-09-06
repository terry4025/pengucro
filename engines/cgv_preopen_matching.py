from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from engines.cgv_client import normalize_time, schedule_items
from engines.cgv_movie_identity import (
    schedule_matches_movie,
    schedule_matches_movie_title_exact,
)

PREOPEN_TIME_DRIFT_WINDOW_MINUTES = 90

_EXPERIENCE_ALIASES = (
    ("IMAX", ("IMAX", "아이맥스")),
    ("4DX", ("4DX", "포디엑스")),
    ("SCREENX", ("SCREENX", "스크린엑스")),
    ("SPHEREX", ("SPHEREX", "스피어엑스")),
)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _semantic_text(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "")).upper()
    for canonical, aliases in _EXPERIENCE_ALIASES:
        for alias in aliases:
            text = text.replace(alias.upper(), canonical)
    return text


def _experience_family(value: Any) -> str:
    text = _semantic_text(value)
    for canonical, _aliases in _EXPERIENCE_ALIASES:
        if canonical in text:
            return canonical
    return ""


def _dimension(value: Any) -> str:
    text = _semantic_text(value)
    if "3D" in text:
        return "3D"
    if "2D" in text:
        return "2D"
    return ""


def _item_screen_text(item: Mapping[str, Any]) -> str:
    return " ".join(
        str(item.get(key, "") or "") for key in ("expoScnsNm", "scnsNm")
    )


def _item_format_text(item: Mapping[str, Any]) -> str:
    return " ".join(
        str(item.get(key, "") or "")
        for key in (
            "movkndDsplEnm",
            "movkndDsplNm",
            "sbtdivNm",
            "videoAddexpCdNm",
            "expoScnsNm",
            "scnsNm",
        )
    )


def context_matches(
    item: Mapping[str, Any],
    auditorium: str,
    format_name: str,
    *,
    include_controlled: bool = False,
) -> bool:
    """Match the requested auditorium/format without crossing experience families.

    CGV occasionally changes display labels at publication time, for example
    ``IMAX관 · IMAX LASER 2D`` to ``IMAX · IMAX 2D``.  Exact/substring matching
    remains preferred, but known premium-format families may bridge harmless
    label drift.  A requested IMAX screening can never fall through to regular
    2D/3D because the experience family must still agree.
    """

    screen_text = _item_screen_text(item)
    format_text = _item_format_text(item)
    screen_key = _compact(screen_text)
    item_format_key = _compact(format_text)

    auditorium_key = _compact(auditorium)
    if auditorium_key:
        direct = auditorium_key in screen_key or auditorium_key in item_format_key
        if not direct:
            target_family = _experience_family(auditorium)
            item_family = _experience_family(f"{screen_text} {format_text}")
            if not target_family or item_family != target_family:
                return False

    format_key = _compact(format_name)
    if format_key and format_key not in item_format_key:
        target_family = _experience_family(format_name)
        item_family = _experience_family(format_text)
        if not target_family or item_family != target_family:
            return False
        target_dimension = _dimension(format_name)
        item_dimension = _dimension(format_text)
        if target_dimension and item_dimension and target_dimension != item_dimension:
            return False

    if not include_controlled and str(item.get("cntlYn", "N") or "N").upper() == "Y":
        return False
    return True


def has_booking_identity(item: Mapping[str, Any]) -> bool:
    def present(value: Any) -> bool:
        return value is not None and bool(str(value).strip())

    return all(
        present(item.get(key))
        for key in ("siteNo", "scnYmd", "scnsNo", "scnSseq")
    )


def matching_schedule_candidates(
    payload: Mapping[str, Any],
    *,
    movie: str,
    mov_no: str = "",
    auditorium: str = "",
    format_name: str = "",
    include_controlled: bool = False,
) -> list[dict[str, Any]]:
    """Return selectable rows, preferring the saved CGV movie identity.

    Once a pre-open request has a ``movNo``, another published ``movNo`` can
    never be accepted merely because its display title is the same. Rows whose
    movie ID has not been published yet remain usable only through an exact
    normalized-title match. Exact-ID rows take precedence over those temporary
    title fallbacks whenever both are present.
    """

    target_mov_no = str(mov_no or "").strip().casefold()
    exact_identity: list[dict[str, Any]] = []
    title_fallbacks: list[dict[str, Any]] = []
    for raw in schedule_items(payload):
        item = dict(raw)
        row_mov_no = str(item.get("movNo") or item.get("mov_no") or "").strip()
        if target_mov_no:
            if row_mov_no:
                if row_mov_no.casefold() != target_mov_no:
                    continue
                destination = exact_identity
            else:
                if not schedule_matches_movie_title_exact(item, movie, format_name):
                    continue
                destination = title_fallbacks
        else:
            if not schedule_matches_movie(item, movie, format_name):
                continue
            destination = title_fallbacks
        if not context_matches(
            item,
            auditorium,
            format_name,
            include_controlled=include_controlled,
        ):
            continue
        if not has_booking_identity(item):
            continue
        destination.append(item)
    return exact_identity or title_fallbacks


def _time_minutes(value: Any) -> int | None:
    normalized = normalize_time(value)
    if len(normalized) != 4 or not normalized.isdigit():
        return None
    hour = int(normalized[:2])
    minute = int(normalized[2:])
    if hour > 29 or minute > 59:
        return None
    return hour * 60 + minute


def _time_distance_minutes(left: int, right: int) -> int:
    direct = abs(left - right)
    return min(direct, abs((left + 1440) - right), abs(left - (right + 1440)))


def _schedule_identity(item: Mapping[str, Any]) -> tuple[str, ...]:
    core = tuple(
        str(item.get(key, "") or "")
        for key in ("siteNo", "scnYmd", "scnsNo", "scnSseq")
    )
    if core[2] or core[3]:
        return core
    return (*core, str(item.get("scnsrtTm", "") or ""))


def has_published_seat_inventory(item: Mapping[str, Any]) -> bool:
    """Return whether CGV reports sellable seats for a published screening.

    Pre-open publication can list a screening whose seat counters are all
    zero (frSeatCnt=0 with stcnt>0) before sales actually begin. Such rows
    keep their booking identity but are demoted below identical rows that
    report stock. Rows without counter fields are treated as bookable so the
    historical payloads and cancellation-ticket targets stay unaffected.
    """

    def digits(value):
        text = "" if value is None else str(value).strip()
        return int(text) if text.isdigit() else None

    free = digits(item.get("frSeatCnt"))
    total = digits(item.get("stcnt"))
    if free is None or total is None:
        return True
    if total <= 0:
        return True
    return free > 0


def _chronological_key(item: Mapping[str, Any]) -> tuple[int, tuple[str, ...]]:
    minutes = _time_minutes(item.get("scnsrtTm"))
    return (minutes if minutes is not None else 10**9, _schedule_identity(item))


def _normalized_preferences(preferred_times: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in preferred_times:
        normalized = normalize_time(value)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def rank_preopen_schedules(
    candidates: Iterable[Mapping[str, Any]],
    preferred_times: Iterable[str] = (),
    *,
    drift_window_minutes: int = PREOPEN_TIME_DRIFT_WINDOW_MINUTES,
    include_zero_inventory: bool = False,
) -> list[dict[str, Any]]:
    """Map reference-day time priorities onto the real published schedule.

    One nearest real screening is assigned to each saved reference-time priority
    while the drift stays within the bounded window. Additional matching slots
    are retained only inside that same window, giving the seat-priority ladder
    safe alternatives without silently booking a completely different time.
    """

    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for raw in candidates:
        item = dict(raw)
        unique.setdefault(_schedule_identity(item), item)
    # Preview callers retain the positive-inventory filter. The live preopen
    # ladder explicitly includes zero aggregates and validates fresh real seats
    # before any hold. Publication metadata alone never proves availability.
    remaining = sorted(
        (
            item
            for item in unique.values()
            if include_zero_inventory or has_published_seat_inventory(item)
        ),
        key=_chronological_key,
    )
    if not remaining:
        return []

    preferences = _normalized_preferences(preferred_times)
    if not preferences:
        return remaining

    ordered: list[dict[str, Any]] = []
    for preferred in preferences:
        target = _time_minutes(preferred)
        if target is None or not remaining:
            continue
        ranked: list[tuple[int, tuple[int, tuple[str, ...]], dict[str, Any]]] = []
        for item in remaining:
            observed = _time_minutes(item.get("scnsrtTm"))
            if observed is None:
                continue
            ranked.append((_time_distance_minutes(observed, target), _chronological_key(item), item))
        if not ranked:
            continue
        ranked.sort(key=lambda value: (value[0], value[1]))
        delta, _key, winner = ranked[0]
        if delta <= max(0, int(drift_window_minutes)):
            ordered.append(winner)
            remaining.remove(winner)

    if remaining:
        preference_minutes = [
            value for value in (_time_minutes(pref) for pref in preferences) if value is not None
        ]

        def fallback_distance(item: Mapping[str, Any]) -> int:
            observed = _time_minutes(item.get("scnsrtTm"))
            if observed is None or not preference_minutes:
                return 10**9
            return min(
                _time_distance_minutes(observed, target)
                for target in preference_minutes
            )

        # Keep additional real slots only when they are still plausibly the
        # shifted counterpart of at least one saved reference time. This gives
        # the seat-priority ladder alternatives without silently booking a
        # completely different morning/evening screening.
        remaining = [
            item
            for item in remaining
            if fallback_distance(item) <= max(0, int(drift_window_minutes))
        ]
        remaining.sort(
            key=lambda item: (fallback_distance(item), *_chronological_key(item))
        )
        ordered.extend(remaining)

    return ordered


def select_preopen_schedule(
    payload: Mapping[str, Any],
    *,
    movie: str,
    mov_no: str = "",
    show_time: str = "",
    auditorium: str = "",
    preferred_times: Iterable[str] = (),
    format_name: str = "",
    drift_window_minutes: int = PREOPEN_TIME_DRIFT_WINDOW_MINUTES,
    include_zero_inventory: bool = False,
    require_movie_id: bool = False,
) -> dict[str, Any] | None:
    preferred = list(preferred_times)
    if not preferred and show_time:
        preferred = [show_time]
    candidates = matching_schedule_candidates(
        payload,
        movie=movie,
        mov_no=mov_no,
        auditorium=auditorium,
        format_name=format_name,
    )
    if require_movie_id:
        candidates = booking_ready_schedules(candidates, mov_no)
    ranked = rank_preopen_schedules(candidates, preferred,
                                   drift_window_minutes=drift_window_minutes,
                                   include_zero_inventory=include_zero_inventory)
    return ranked[0] if ranked else None


def booking_ready_schedules(candidates, verified_movie_id=""):
    """Use only rows already checked by matching_schedule_candidates.

    That matcher rejects conflicting IDs and requires an exact title when the
    row has no movie ID. Only that validated saved/discovered ID can fill it.
    Unknown IDs keep waiting; never send a blank movie ID to the price API.
    """
    ready = []
    for row in candidates:
        movie_id = str(row.get("movNo") or row.get("mov_no") or verified_movie_id or "").strip()
        if movie_id:
            ready.append(dict(row, movNo=movie_id))
    return ready


def normalize_preopen_time_drift(value: Any) -> int:
    """Only an explicit supported preference can widen the booking time."""
    try:
        minutes = int(str(value))
    except (TypeError, ValueError):
        return 0
    return minutes if minutes in {0, 15, 30, 60, 90} else 0
