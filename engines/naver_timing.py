"""Persist conservative, product-specific Naver opening timing feedback.

The public booking API does not expose the server's request-arrival timestamp.
We therefore learn only from outcomes that have a useful interpretation:

* an explicit NOT_OPEN reply means the request reached the booking gate early;
* a refusal followed by exhausted public inventory is still ambiguous because
  it cannot distinguish an early gate rejection from a competing reservation;
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
TIMING_HISTORY_VERSION = 2
DEFAULT_TARGET_BEFORE_OPEN_SECONDS = 0.060
PREPAID_TARGET_BEFORE_OPEN_SECONDS = 0.020
MIN_TARGET_BEFORE_OPEN_SECONDS = 0.010
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


def _default_target(payment_mode: str = "") -> float:
    return (
        PREPAID_TARGET_BEFORE_OPEN_SECONDS
        if str(payment_mode or "").strip() == "npay_prepaid"
        else DEFAULT_TARGET_BEFORE_OPEN_SECONDS
    )


def _bounded_target(value: Any, payment_mode: str = "") -> float:
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        candidate = _default_target(payment_mode)
    return min(
        (PREPAID_TARGET_BEFORE_OPEN_SECONDS
         if payment_mode == "npay_prepaid" else MAX_TARGET_BEFORE_OPEN_SECONDS),
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
        return NaverTimingProfile("", _default_target(payment_mode), 0)
    payload = load_json(
        TIMING_HISTORY_FILE,
        {"version": TIMING_HISTORY_VERSION, "entries": {}},
    )
    entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    row = entries.get(key, {}) if isinstance(entries, dict) else {}
    observations = row.get("observations", []) if isinstance(row, dict) else []
    # v1 could move a profile earlier after one RT47 + sold-out observation.
    # That evidence is ambiguous, so do not carry the learned lead into v2.
    if isinstance(row, dict) and payload.get("version") == TIMING_HISTORY_VERSION:
        stored_target = row.get("target_before_open_seconds")
    elif isinstance(row, dict) and payment_mode != "npay_prepaid":
        # v1 postpaid learning came from explicit gate evidence and is safe to
        # preserve. Only prepaid's ambiguous early-send learning is reset.
        stored_target = row.get("target_before_open_seconds")
    else:
        stored_target = _default_target(payment_mode)
    return NaverTimingProfile(
        key,
        _bounded_target(stored_target, payment_mode),
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
        profile = NaverTimingProfile("", _default_target(payment_mode), 0)
        return NaverTimingUpdate(profile, profile.target_before_open_seconds, 0.0)

    clean_outcome = str(outcome or "unknown")[:40]
    clean_code = str(response_code or "")[:80]
    result: dict[str, Any] = {}

    def updater(current: Any) -> dict[str, Any]:
        payload = dict(current) if isinstance(current, dict) else {}
        entries = payload.get("entries")
        entries = dict(entries) if isinstance(entries, dict) else {}
        # Migrate every prepaid row before changing the shared schema version.
        # Otherwise recording product A labels untouched v1 rows B/C as v2 and
        # revives the unsafe early targets on their next load.
        if payload.get("version") != TIMING_HISTORY_VERSION:
            for entry_key, entry in list(entries.items()):
                if str(entry_key).endswith("|npay_prepaid") and isinstance(entry, dict):
                    entries[entry_key] = {
                        **entry,
                        "target_before_open_seconds": PREPAID_TARGET_BEFORE_OPEN_SECONDS,
                    }
        row = entries.get(key)
        row = dict(row) if isinstance(row, dict) else {}
        version = payload.get("version")
        if version == TIMING_HISTORY_VERSION or payment_mode != "npay_prepaid":
            stored_previous = row.get("target_before_open_seconds")
        else:
            stored_previous = _default_target(payment_mode)
        previous = _bounded_target(stored_previous, payment_mode)

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
            # A refusal plus exhausted inventory cannot tell whether this request
            # reached the gate early or another customer won the one-seat race.
            # Preserve the target instead of amplifying one noisy observation.

        updated_target = _bounded_target(previous + adjustment, payment_mode)
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
            "clock_bridge_rtt_ms",
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
        payload["version"] = TIMING_HISTORY_VERSION
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
        {"version": TIMING_HISTORY_VERSION, "entries": {}},
    )
    profile = NaverTimingProfile(key, result["target"], result["count"])
    return NaverTimingUpdate(profile, result["previous"], result["adjustment"])
