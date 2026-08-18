from __future__ import annotations

import math
from typing import Iterable


YONGSAN_IMAX_SITE_NO = "0013"
YONGSAN_IMAX_CENTER_PAIR = (22, 23)
YONGSAN_IMAX_CENTER = sum(YONGSAN_IMAX_CENTER_PAIR) / 2.0

# These rows mirror the dedicated Yongsan IMAX guide already used by Pengucro:
# H = balanced, F-G = immersive, I-J = comfortable.  The explicit order for
# "best" follows the existing recommendation tiers (H first, then G/I, then
# F/J).  These presets are only used before CGV exposes a real seat map; once a
# real map is available the normal coordinate/aisle-aware recommendation code
# remains authoritative.
YONGSAN_IMAX_MODE_ROWS: dict[str, tuple[str, ...]] = {
    "balanced": ("H",),
    "immersive": ("F", "G"),
    "comfortable": ("I", "J"),
    "best": ("H", "G", "I", "F", "J"),
    "recommended": ("G", "I", "H", "F", "J"),
    "preference": ("F", "J", "G", "I", "H"),
}


def is_yongsan_imax_target(
    site_no: str,
    auditorium: str = "",
    format_name: str = "",
) -> bool:
    """Return whether the selector is targeting Yongsan's IMAX auditorium."""

    if str(site_no or "").strip() != YONGSAN_IMAX_SITE_NO:
        return False
    context = f"{auditorium or ''} {format_name or ''}".upper()
    return "IMAX" in context


def _centered_starts(people: int, *, radius: int = 2) -> tuple[int, ...]:
    """Return central contiguous-block starts ordered by distance to seats 22/23."""

    count = max(1, min(int(people), 8))
    ideal = YONGSAN_IMAX_CENTER - (count - 1) / 2.0
    lower = math.floor(ideal)
    upper = math.ceil(ideal)

    starts: list[int] = []
    for offset in range(0, max(0, int(radius)) + 1):
        candidates: Iterable[int]
        if offset == 0:
            candidates = (lower, upper)
        else:
            candidates = (lower - offset, upper + offset)
        for start in candidates:
            if start > 0 and start not in starts:
                starts.append(start)

    return tuple(
        sorted(
            starts,
            key=lambda start: (
                abs((start + (count - 1) / 2.0) - YONGSAN_IMAX_CENTER),
                start,
            ),
        )
    )


def yongsan_imax_preopen_groups(
    mode: str,
    people: int,
    *,
    variants_per_row: int = 3,
) -> tuple[tuple[str, ...], ...]:
    """Build ranked concrete Yongsan IMAX groups without loading a seat map.

    The hard-coded information is intentionally narrow: the known central pair
    (22/23) and the dedicated recommendation rows.  No availability assumption
    is made.  At booking time the normal CGV seat API still validates that every
    concrete seat exists, is sellable, is contiguous, and is currently free;
    the existing auto-seat strategy remains a final runtime fallback.
    """

    count = max(1, min(int(people), 8))
    rows = YONGSAN_IMAX_MODE_ROWS.get(str(mode or "").strip().lower(), ())
    if not rows:
        return ()

    starts = _centered_starts(count)
    per_row = max(1, min(int(variants_per_row), len(starts)))
    result: list[tuple[str, ...]] = []
    for row in rows:
        for start in starts[:per_row]:
            group = tuple(f"{row}{number}" for number in range(start, start + count))
            if group not in result:
                result.append(group)
    return tuple(result)
