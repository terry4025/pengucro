from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

import engines.cgv_engine as _base_engine_module
import engines.cgv_engine_priority_ladder as _priority_ladder_module
from engines.cgv_client import (
    normalize_time,
    schedule_items,
    select_schedule as _legacy_select_schedule,
)
from engines.cgv_engine_priority_ladder_runtime import CgvEngine as PriorityLadderRuntimeCgvEngine
from engines.cgv_movie_identity import schedule_matches_movie


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _context_matches(
    item: Mapping[str, Any], auditorium: str, format_name: str
) -> bool:
    auditorium_key = _compact(auditorium)
    format_key = _compact(format_name)
    screen_text = " ".join(
        str(item.get(key, "") or "") for key in ("expoScnsNm", "scnsNm")
    )
    format_text = " ".join(
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
    screen_key = _compact(screen_text)
    item_format_key = _compact(format_text)
    if auditorium_key and auditorium_key not in screen_key and auditorium_key not in item_format_key:
        return False
    if format_key and format_key not in item_format_key:
        return False
    if str(item.get("cntlYn", "N")).upper() == "Y":
        return False
    return True


def select_schedule(
    payload: Mapping[str, Any],
    *,
    movie: str,
    show_time: str = "",
    auditorium: str = "",
    preferred_times: Iterable[str] = (),
    format_name: str = "",
) -> dict[str, Any] | None:
    """Keep the proven selector first, then bridge pre-open display-name drift.

    Existing open-date behavior is intentionally preserved.  The canonical path
    runs only if the historical selector returns no schedule, which covers the
    risky transition from e.g. ``오디세이(IMAX LASER 2D)`` saved pre-open to
    ``movNm=오디세이`` when CGV publishes the real screening IDs.
    """

    legacy = _legacy_select_schedule(
        payload,
        movie=movie,
        show_time=show_time,
        auditorium=auditorium,
        preferred_times=preferred_times,
        format_name=format_name,
    )
    if legacy is not None:
        return legacy

    preferred = [normalize_time(value) for value in preferred_times if normalize_time(value)]
    if not preferred and show_time:
        preferred = [normalize_time(show_time)]

    candidates: list[dict[str, Any]] = []
    for item in schedule_items(payload):
        if not schedule_matches_movie(item, movie, format_name):
            continue
        if not _context_matches(item, auditorium, format_name):
            continue
        candidates.append(item)

    if not candidates:
        return None
    if preferred:
        for target_time in preferred:
            for item in candidates:
                if normalize_time(item.get("scnsrtTm")) == target_time:
                    return item
        return None
    return candidates[0]


def _has_schedule_hint(
    payload: Any, movie: str, auditorium: str = ""
) -> bool:
    if not isinstance(payload, dict):
        return False
    auditorium_key = _compact(auditorium)
    for item in schedule_items(payload):
        item_format = str(
            item.get("movkndDsplEnm") or item.get("movkndDsplNm") or ""
        )
        if not schedule_matches_movie(item, movie, item_format):
            continue
        if auditorium_key:
            screen_text = " ".join(
                str(item.get(key, "") or "")
                for key in ("expoScnsNm", "scnsNm", "movkndDsplEnm", "movkndDsplNm")
            )
            if auditorium_key not in _compact(screen_text):
                continue
        return True
    return False


# The mature base reservation loop and the priority ladder both resolve these
# functions from their module globals at runtime. Installing the bridge here
# avoids copying either large engine while keeping all hold/checkout behavior
# unchanged.
_base_engine_module.select_schedule = select_schedule
_base_engine_module._has_schedule_hint = _has_schedule_hint
_priority_ladder_module.select_schedule = select_schedule


class CgvEngine(PriorityLadderRuntimeCgvEngine):
    """Final CGV runtime with stable pre-open -> real movie identity matching."""

    pass
