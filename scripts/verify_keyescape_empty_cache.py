"""Live read-only verification for Keyescape's completely empty cache path."""

from __future__ import annotations

import os
import json
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from engines.keyescape_engine import KeyescapeEngine
from engines.keyescape_timetable_collector import KeyescapeTimetableCollector
from pengucro.storage import load_json


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pengucro-keyescape-empty-") as folder:
        os.environ["PENGUCRO_DATA_DIR"] = folder
        collector = KeyescapeTimetableCollector()
        started = time.monotonic()
        result = collector.collect()
        elapsed = time.monotonic() - started
        cache = load_json("keyescape_slot_templates.json", {"entries": {}})
        entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
        seed = json.loads(
            (Path(__file__).resolve().parents[1] / "keyescape_slot_templates_seed.json")
            .read_text(encoding="utf-8")
        )
        seed_entries = seed.get("entries", {}) if isinstance(seed, dict) else {}
        compared = 0
        mismatches = []
        for key, live_rows in entries.items() if isinstance(entries, dict) else []:
            historical_rows = seed_entries.get(key, []) if isinstance(seed_entries, dict) else []
            by_date = {
                str(row.get("date", "")): row
                for row in historical_rows if isinstance(row, dict)
            }
            for live_row in live_rows if isinstance(live_rows, list) else []:
                historical = by_date.get(str(live_row.get("date", "")))
                if not historical:
                    continue
                historical_slots = historical.get("slots", {})
                for stamp, slot_id in live_row.get("slots", {}).items():
                    if stamp not in historical_slots:
                        continue
                    compared += 1
                    if str(slot_id) != str(historical_slots[stamp]):
                        mismatches.append(
                            (key, live_row.get("date"), stamp, historical_slots[stamp], slot_id)
                        )
        engine = KeyescapeEngine(lambda *_args: None)
        armed = []
        for key, rows in entries.items() if isinstance(entries, dict) else []:
            try:
                _site, branch_id, theme_num = key.rsplit("|", 2)
            except ValueError:
                continue
            for row in rows if isinstance(rows, list) else []:
                try:
                    source_day = datetime.strptime(row["date"], "%Y-%m-%d").date()
                except (KeyError, TypeError, ValueError):
                    continue
                # Friday/Saturday/Sunday use the site's explicit B/C/D marker
                # and one recent complete public schedule.
                if source_day.weekday() < 4:
                    continue
                target_day = source_day + timedelta(days=7)
                slots = row.get("slots", {})
                for stamp in slots if isinstance(slots, dict) else []:
                    slot_id, sources = engine._trusted_slot_from_cache(
                        target_day.isoformat(), stamp, branch_id, theme_num
                    )
                    if slot_id:
                        armed.append(
                            (branch_id, theme_num, target_day.isoformat(), stamp, slot_id, sources)
                        )
                        break
                if armed and armed[-1][0:2] == (branch_id, theme_num):
                    break

        print(f"temporary_data_dir={folder}")
        print(f"branches={result.branch_count}")
        print(f"themes={result.theme_count}")
        print(f"requests={result.request_count}")
        print(f"saved={result.saved_count}")
        print(f"unavailable={result.unavailable_count}")
        print(f"failed={result.failed_count}")
        print(f"coverage={result.coverage}")
        print(f"cache_keys={len(entries) if isinstance(entries, dict) else 0}")
        print(f"fast_path_themes={len(armed)}")
        print(f"historical_id_comparisons={compared}")
        print(f"historical_id_mismatches={len(mismatches)}")
        for mismatch in mismatches[:10]:
            print("mismatch=" + "|".join(map(str, mismatch)))
        print(f"elapsed_seconds={elapsed:.2f}")
        for example in armed[:5]:
            print("example=" + "|".join(map(str, example)))
        return 0 if (
            result.saved_count and armed and not result.failed_count
            and compared and not mismatches
        ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
