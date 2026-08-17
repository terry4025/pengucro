"""Persistent Keyescape timetable templates shared by picker and engine."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone

from pengucro.storage import load_json, save_json


KST = timezone(timedelta(hours=9))
SLOT_TEMPLATE_FILE = "keyescape_slot_templates.json"
SLOT_TEMPLATE_LIMIT = 40
PLACEHOLDER_SLOT_ID = "9999"
_cache_lock = threading.Lock()


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

    key = f"{str(site_url).rstrip('/').lower()}|{zizum_num}|{theme_num}"
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
