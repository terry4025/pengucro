from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from engines.cgv_client import normalize_time, schedule_items
from engines.cgv_movie_identity import schedule_matches_movie

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
    ``IMAX관 · IMAX LASER 2D`` to ``IMAX · IMAX 2D``. Exact/substring matching
    remains preferred, but known premium-format families may bridge harmless
    label drift. A requested IMAX screening can never fall through to regular
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


def matching_schedule_candidates(
    payload: Mapping[str, Any],
    *,
    movie: str,
    auditorium: str = "",
    format_name: str = "",
    include_controlled: bool = False,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for raw in schedule_items(payload):
        item = dict(raw)
        if not schedule_matches_movie(item, movie, format_name):
            continue
        if not context_matches(
            item,
            auditorium,
            format_name,
            include_controlled=include_controlled,
        ):
            continue
        candidates.append(item)
    return candidates


def _time_minutes(value: Any) -> int | None:
    normalized = normalize_time(value)
    if len(normalized) != 4 or not normalized.isdigit():
        return None
    hour = int(normalized[:2])
    minute = int(normalized[2:])
    if hour > 29 or minute > 59:
        return None
    return hour * 60 + minute


def _schedule_identity(item: Mapping[str, Any]) -> tuple[str, ...]:
    core = tuple(
        str(item.get(key, "") or "")
        for key in ("siteNo", "scnYmd", "scnsNo", "scnSseq")
    )
    if core[2] or core[3]:
        return core
    return (*core, str(item.get("scnsrtTm", "") or ""))


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
) -> list[dict[str, Any]]:
    """Map reference-day time priorities onto the real published schedule.

    One nearest real screening is assigned to each saved reference-time priority
    while the drift stays within the bounded window. Any remaining matching
    screenings are appended as safety fallbacks so a changed/extra target-date
    slot can still win when the primary mapped slots have no usable seats.
    """

    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for raw in candidates:
        item = dict(raw)
        unique.setdefault(_schedule_identity(item), item)
    remaining = sorted(unique.values(), key=_chronological_key)
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
            ranked.append((abs(observed - target), _chronological_key(item), item))
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

        def fallback_key(item: Mapping[str, Any]):
            observed = _time_minutes(item.get("scnsrtTm"))
            if observed is None or not preference_minutes:
                return (10**9, *_chronological_key(item))
            return (
                min(abs(observed - target) for target in preference_minutes),
                *_chronological_key(item),
            )

        remaining.sort(key=fallback_key)
        ordered.extend(remaining)

    return ordered


def select_preopen_schedule(
    payload: Mapping[str, Any],
    *,
    movie: str,
    show_time: str = "",
    auditorium: str = "",
    preferred_times: Iterable[str] = (),
    format_name: str = "",
) -> dict[str, Any] | None:
    preferred = list(preferred_times)
    if not preferred and show_time:
        preferred = [show_time]
    candidates = matching_schedule_candidates(
        payload,
        movie=movie,
        auditorium=auditorium,
        format_name=format_name,
    )
    ranked = rank_preopen_schedules(candidates, preferred)
    return ranked[0] if ranked else None
