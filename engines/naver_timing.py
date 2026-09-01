"""Persist conservative, product-specific Naver opening timing feedback.

The public booking API does not expose the server's request-arrival timestamp.
We therefore learn only from outcomes that have a useful interpretation:

* an explicit NOT_OPEN reply means the request reached the booking gate early;
* a refusal followed by exhausted public inventory and no booking evidence means
  the next run may benefit from a slightly earlier arrival target;
* a confirmed booking keeps the successful target unchanged.

The adjustment is intentionally small and bounded.  One noisy opening must not
turn into a large global offset, and pre-paid/post-paid products never share a
profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pengucro.storage import load_json, update_json


TIMING_HISTORY_FILE = "naver_timing_history.json"
DEFAULT_TARGET_BEFORE_OPEN_SECONDS = 0.060
MIN_TARGET_BEFORE_OPEN_SECONDS = 0.020
MAX_TARGET_BEFORE_OPEN_SECONDS = 0.120
ADJUSTMENT_STEP_SECONDS = 0.010
MAX_OBSERVATIONS = 24


@dataclass(frozen=True)
class NaverTimingProfile:
    key: str
    target_before_open_seconds: float
    observation_count: int = 0


@dataclass(frozen=True)
class NaverTimingUpdate:
    profile: NaverTimingProfile
    previous_target_seconds: float
    adjustment_seconds: float


def _bounded_target(value: Any) -> float:
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        candidate = DEFAULT_TARGET_BEFORE_OPEN_SECONDS
    return min(
        MAX_TARGET_BEFORE_OPEN_SECONDS,
        max(MIN_TARGET_BEFORE_OPEN_SECONDS, candidate),
    )


def timing_profile_key(
    business_id: str,
    biz_item_id: str,
    payment_mode: str,
) -> str:
    values = tuple(str(value or "").strip() for value in (
        business_id,
        biz_item_id,
        payment_mode,
    ))
    if not all(values):
        return ""
    return "|".join(values)


def load_timing_profile(
    business_id: str,
    biz_item_id: str,
    payment_mode: str,
) -> NaverTimingProfile:
    key = timing_profile_key(business_id, biz_item_id, payment_mode)
    if not key:
        return NaverTimingProfile("", DEFAULT_TARGET_BEFORE_OPEN_SECONDS, 0)
    payload = load_json(TIMING_HISTORY_FILE, {"version": 1, "entries": {}})
    entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    row = entries.get(key, {}) if isinstance(entries, dict) else {}
    observations = row.get("observations", []) if isinstance(row, dict) else []
    return NaverTimingProfile(
        key,
        _bounded_target(
            row.get("target_before_open_seconds")
            if isinstance(row, dict)
            else DEFAULT_TARGET_BEFORE_OPEN_SECONDS
        ),
        len(observations) if isinstance(observations, list) else 0,
    )


def record_timing_observation(
    business_id: str,
    biz_item_id: str,
    payment_mode: str,
    *,
    outcome: str,
    response_code: str = "",
    booking_confirmed: bool = False,
    inventory_remaining: int | None = None,
    timing: dict[str, Any] | None = None,
) -> NaverTimingUpdate:
    key = timing_profile_key(business_id, biz_item_id, payment_mode)
    if not key:
        profile = NaverTimingProfile("", DEFAULT_TARGET_BEFORE_OPEN_SECONDS, 0)
        return NaverTimingUpdate(profile, profile.target_before_open_seconds, 0.0)

    clean_outcome = str(outcome or "unknown")[:40]
    clean_code = str(response_code or "")[:80]
    result: dict[str, Any] = {}

    def updater(current: Any) -> dict[str, Any]:
        payload = dict(current) if isinstance(current, dict) else {}
        entries = payload.get("entries")
        entries = dict(entries) if isinstance(entries, dict) else {}
        row = entries.get(key)
        row = dict(row) if isinstance(row, dict) else {}
        previous = _bounded_target(row.get("target_before_open_seconds"))

        adjustment = 0.0
        if clean_outcome in {"success_after_notopen", "refused_after_notopen"}:
            # An explicit resolver NOT_OPEN is direct evidence that the initial
            # request reached the gate early. It outweighs an indirect sold-out
            # observation after the boundary, even if a boundary retry succeeded.
            adjustment = -ADJUSTMENT_STEP_SECONDS
        elif not booking_confirmed:
            if clean_outcome == "notopen":
                # The booking resolver explicitly confirmed that the gate had not
                # opened. Move the next target closer to/after the boundary.
                adjustment = -ADJUSTMENT_STEP_SECONDS
            elif clean_outcome == "refused" and inventory_remaining == 0:
                # No own booking appeared and inventory was already exhausted.
                # This is the only refusal state that supports a small earlier
                # adjustment; available/unknown inventory remains ambiguous.
                adjustment = ADJUSTMENT_STEP_SECONDS

        updated_target = _bounded_target(previous + adjustment)
        observations = row.get("observations")
        observations = list(observations) if isinstance(observations, list) else []
        raw_timing = timing if isinstance(timing, dict) else {}
        safe_timing = {}
        for field in (
            "dispatch_offset_ms",
            "estimated_arrival_offset_ms",
            "last_dispatch_offset_ms",
            "last_estimated_arrival_offset_ms",
            "transport_rtt_ms",
            "submit_rtt_ms",
            "clock_rtt_ms",
            "clock_uncertainty_ms",
            "clock_spread_ms",
            "ttfb_ms",
            "response_ms",
            "attempts",
            "not_open_attempts",
            "http_status",
        ):
            value = raw_timing.get(field)
            if isinstance(value, (int, float)):
                safe_timing[field] = round(float(value), 3)
        observations.append({
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "outcome": clean_outcome,
            "response_code": clean_code,
            "booking_confirmed": bool(booking_confirmed),
            "inventory_remaining": (
                int(inventory_remaining)
                if isinstance(inventory_remaining, (int, float))
                else None
            ),
            "target_before_open_seconds": round(previous, 3),
            "adjustment_seconds": round(adjustment, 3),
            "timing": safe_timing,
        })
        observations = observations[-MAX_OBSERVATIONS:]
        entries[key] = {
            "target_before_open_seconds": round(updated_target, 3),
            "observations": observations,
        }
        payload["version"] = 1
        payload["entries"] = entries
        result.update(
            previous=previous,
            adjustment=updated_target - previous,
            target=updated_target,
            count=len(observations),
        )
        return payload

    update_json(
        TIMING_HISTORY_FILE,
        updater,
        {"version": 1, "entries": {}},
    )
    profile = NaverTimingProfile(key, result["target"], result["count"])
    return NaverTimingUpdate(profile, result["previous"], result["adjustment"])
