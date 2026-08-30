"""Persistent Keyescape timetable templates shared by picker and engine."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pengucro.storage import load_json, save_json


KST = timezone(timedelta(hours=9))
SLOT_TEMPLATE_FILE = "keyescape_slot_templates.json"
SLOT_TEMPLATE_LIMIT = 40
PLACEHOLDER_SLOT_ID = "9999"
BUNDLED_SEED_FILENAME = "keyescape_slot_templates_seed.json"
BUNDLED_SEED_MAX_PAST_DAYS = 21
BUNDLED_SEED_MAX_FUTURE_DAYS = 14
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class SeedMergeResult:
    imported: int = 0
    retained: int = 0
    rejected: int = 0
    available: bool = False


def _canonical_site_url(site_url: str) -> str:
    value = str(site_url).rstrip("/").lower()
    for suffix in ("/reservation.php", "/reservation2.php"):
        if value.endswith(suffix):
            return value[:-len(suffix)]
    return value


def _slot_time(slot) -> str:
    try:
        return f"{int(slot.get('hh', 0)):02d}:{int(slot.get('mm', 0)):02d}"
    except (AttributeError, TypeError, ValueError):
        return ""


def _schedule_group(day) -> str:
    return "mon_thu" if day.weekday() <= 3 else f"weekday_{day.weekday()}"


def _slot_template_gubun(slots) -> str:
    values = {
        str(row.get("gubun", "") or "").strip().upper()
        for row in slots or []
        if isinstance(row, dict)
        and _slot_time(row)
        and str(row.get("num", "") or "")
        and str(row.get("num", "") or "") != PLACEHOLDER_SLOT_ID
    }
    values.discard("")
    return next(iter(values)) if len(values) == 1 else ""


def _slot_template_payload(slots) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    for row in slots or []:
        stamp = _slot_time(row)
        slot_id = str(row.get("num", "") or "") if isinstance(row, dict) else ""
        if stamp and slot_id and slot_id != PLACEHOLDER_SLOT_ID:
            mapping[stamp] = slot_id
    if len(mapping) < 2:
        return "", {}
    canonical = json.dumps(
        sorted(mapping.items()), ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), mapping


def _mapping_signature(mapping) -> str:
    if not isinstance(mapping, dict) or len(mapping) < 2:
        return ""
    cleaned = {
        str(stamp): str(slot_id)
        for stamp, slot_id in mapping.items()
        if str(stamp) and str(slot_id) and str(slot_id) != PLACEHOLDER_SLOT_ID
    }
    if len(cleaned) < 2:
        return ""
    canonical = json.dumps(
        sorted(cleaned.items()), ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _seed_payload_digest(entries) -> str:
    canonical = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def bundled_seed_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / BUNDLED_SEED_FILENAME
    return Path(__file__).resolve().parents[1] / BUNDLED_SEED_FILENAME


def build_bundled_seed_payload(cache, reference_day=None) -> dict:
    """Return a bounded, self-checking seed made only from observed cache rows."""
    if reference_day is None:
        reference_day = datetime.now(KST).date()
    entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
    selected = {}
    if isinstance(entries, dict):
        for key, rows in sorted(entries.items()):
            if not isinstance(key, str) or not isinstance(rows, list):
                continue
            try:
                site_url, zizum_num, theme_num = key.rsplit("|", 2)
            except ValueError:
                continue
            canonical_site = _canonical_site_url(site_url)
            if canonical_site != "https://www.keyescape.com":
                continue
            canonical_key = f"{canonical_site}|{zizum_num}|{theme_num}"
            kept = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    source_day = datetime.strptime(
                        str(row.get("date", "")), "%Y-%m-%d"
                    ).date()
                except ValueError:
                    continue
                delta = (source_day - reference_day).days
                if not (-BUNDLED_SEED_MAX_PAST_DAYS <= delta <= BUNDLED_SEED_MAX_FUTURE_DAYS):
                    continue
                slots = row.get("slots")
                signature = _mapping_signature(slots)
                if not signature or signature != str(row.get("signature", "")):
                    continue
                if str(row.get("group", "")) != _schedule_group(source_day):
                    continue
                kept.append({
                    "date": source_day.isoformat(),
                    "weekday": source_day.weekday(),
                    "group": _schedule_group(source_day),
                    "gubun": str(row.get("gubun", "") or "").strip().upper(),
                    "signature": signature,
                    "slots": {
                        str(stamp): str(slot_id)
                        for stamp, slot_id in sorted(slots.items())
                    },
                    "observed_at": str(row.get("observed_at", "") or ""),
                })
            if kept:
                existing = {
                    item["date"]: item
                    for item in selected.get(canonical_key, [])
                }
                for item in kept:
                    previous = existing.get(item["date"])
                    if (
                        previous is None
                        or item.get("observed_at", "") >= previous.get("observed_at", "")
                    ):
                        existing[item["date"]] = item
                selected[canonical_key] = sorted(
                    existing.values(), key=lambda item: item["date"]
                )
    return {
        "version": 1,
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "entries": selected,
        "payload_sha256": _seed_payload_digest(selected),
    }


def merge_bundled_slot_templates(path: Path | None = None) -> SeedMergeResult:
    """Import missing verified rows without overwriting a user's live cache.

    The seed lives inside the versioned executable, whose hash is covered by
    the signed updater manifest.  Its own digest catches accidental packaging
    damage before any row reaches the runtime cache.
    """
    seed_path = Path(path) if path is not None else bundled_seed_path()
    try:
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return SeedMergeResult()
    entries = seed.get("entries", {}) if isinstance(seed, dict) else {}
    if (
        not isinstance(entries, dict)
        or str(seed.get("payload_sha256", "")) != _seed_payload_digest(entries)
    ):
        return SeedMergeResult(available=True, rejected=1)

    today = datetime.now(KST).date()
    imported = retained = rejected = 0
    with _cache_lock:
        cache = load_json(SLOT_TEMPLATE_FILE, {"version": 1, "entries": {}})
        if not isinstance(cache, dict):
            cache = {"version": 1, "entries": {}}
        local_entries = cache.setdefault("entries", {})
        if not isinstance(local_entries, dict):
            local_entries = {}
            cache["entries"] = local_entries

        for key, rows in entries.items():
            if not isinstance(key, str) or not isinstance(rows, list):
                rejected += 1
                continue
            history = local_entries.get(key, [])
            if not isinstance(history, list):
                history = []
            local_dates = {
                str(row.get("date", ""))
                for row in history if isinstance(row, dict)
            }
            for row in rows:
                if not isinstance(row, dict):
                    rejected += 1
                    continue
                try:
                    source_day = datetime.strptime(
                        str(row.get("date", "")), "%Y-%m-%d"
                    ).date()
                except ValueError:
                    rejected += 1
                    continue
                delta = (source_day - today).days
                slots = row.get("slots")
                signature = _mapping_signature(slots)
                try:
                    weekday = int(row.get("weekday", -1))
                except (TypeError, ValueError):
                    weekday = -1
                valid = (
                    -BUNDLED_SEED_MAX_PAST_DAYS <= delta <= BUNDLED_SEED_MAX_FUTURE_DAYS
                    and signature
                    and signature == str(row.get("signature", ""))
                    and str(row.get("group", "")) == _schedule_group(source_day)
                    and weekday == source_day.weekday()
                )
                if not valid:
                    rejected += 1
                    continue
                if source_day.isoformat() in local_dates:
                    retained += 1
                    continue
                history.append(dict(row))
                local_dates.add(source_day.isoformat())
                imported += 1
            if history:
                local_entries[key] = sorted(
                    (row for row in history if isinstance(row, dict)),
                    key=lambda row: str(row.get("date", "")),
                )[-SLOT_TEMPLATE_LIMIT:]
        if imported:
            try:
                save_json(SLOT_TEMPLATE_FILE, cache)
            except OSError:
                return SeedMergeResult(
                    retained=retained, rejected=rejected + imported, available=True
                )
    return SeedMergeResult(
        imported=imported, retained=retained, rejected=rejected, available=True
    )


def remember_slot_template(
    site_url: str,
    target_date: str,
    zizum_num: str,
    theme_num: str,
    slots,
) -> bool:
    """Remember a complete public timetable before its date leaves the window."""
    signature, mapping = _slot_template_payload(slots)
    if not signature:
        return False
    try:
        source_day = datetime.strptime(target_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False

    key = f"{_canonical_site_url(site_url)}|{zizum_num}|{theme_num}"
    with _cache_lock:
        cache = load_json(SLOT_TEMPLATE_FILE, {"version": 1, "entries": {}})
        if not isinstance(cache, dict):
            cache = {"version": 1, "entries": {}}
        entries = cache.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            cache["entries"] = entries
        history = entries.get(key, [])
        if not isinstance(history, list):
            history = []
        history = [
            row for row in history
            if isinstance(row, dict) and row.get("date") != source_day.isoformat()
        ]
        history.append({
            "date": source_day.isoformat(),
            "weekday": source_day.weekday(),
            "group": _schedule_group(source_day),
            "gubun": _slot_template_gubun(slots),
            "signature": signature,
            "slots": mapping,
            "observed_at": datetime.now(KST).isoformat(timespec="seconds"),
        })
        entries[key] = sorted(
            history, key=lambda row: str(row.get("date", ""))
        )[-SLOT_TEMPLATE_LIMIT:]
        try:
            save_json(SLOT_TEMPLATE_FILE, cache)
        except OSError:
            return False
    return True
