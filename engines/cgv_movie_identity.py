from __future__ import annotations

import re
from typing import Any, Mapping


_FORMAT_MARKERS = ("IMAX", "4DX", "SCREENX", "2D", "3D", "LASER", "DOLBY")


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _token_key(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", str(value or "")).casefold()


def strip_matching_format_suffix(title: Any, format_name: Any = "") -> str:
    """Strip a trailing parenthetical only when it is clearly a format suffix.

    CGV can expose the same movie as ``movNm=오디세이`` while ``expoProdNm`` is
    ``오디세이(IMAX LASER 2D)``. Arbitrary title parentheses must remain part
    of the movie name. When the separate format field exists it still has the
    strongest say; when it is temporarily missing during pre-open publication,
    an unmistakable CGV format marker is sufficient to keep movie identity
    stable across the transition.
    """

    text = str(title or "").strip()
    if not text:
        return ""
    match = re.search(r"\s*\(([^()]*)\)\s*$", text)
    if not match:
        return text

    suffix = match.group(1).strip()
    suffix_key = _token_key(suffix)
    if not suffix_key:
        return text

    has_format_marker = any(marker in suffix.upper() for marker in _FORMAT_MARKERS)
    format_key = _token_key(format_name)
    if not format_key:
        return text[: match.start()].strip() if has_format_marker else text

    compatible = suffix_key == format_key or (
        has_format_marker
        and (suffix_key in format_key or format_key in suffix_key)
    )
    return text[: match.start()].strip() if compatible else text


def schedule_movie_name(item: Mapping[str, Any]) -> str:
    """Return CGV's stable human movie title, preferring the real movie field."""

    format_name = str(
        item.get("movkndDsplEnm") or item.get("movkndDsplNm") or ""
    ).strip()
    for key in ("movNm", "prodNm", "expoProdNm"):
        raw = str(item.get(key, "") or "").strip()
        if raw:
            return strip_matching_format_suffix(raw, format_name)
    return ""


def movie_match_key(movie: Any, format_name: Any = "") -> str:
    return _compact(strip_matching_format_suffix(movie, format_name))


def schedule_movie_match_key(item: Mapping[str, Any]) -> str:
    return _compact(schedule_movie_name(item))


def schedule_matches_movie(
    item: Mapping[str, Any], movie: Any, format_name: Any = ""
) -> bool:
    """Match pre-open and real schedules without weakening existing fallbacks."""

    requested = movie_match_key(movie, format_name)
    if not requested:
        return True

    canonical = schedule_movie_match_key(item)
    if canonical and canonical == requested:
        return True

    # Preserve the legacy permissive match as a final compatibility fallback.
    raw_movie_text = " ".join(
        str(item.get(key, "") or "")
        for key in ("movNm", "expoProdNm", "prodNm", "movNo")
    )
    return requested in _compact(raw_movie_text)
