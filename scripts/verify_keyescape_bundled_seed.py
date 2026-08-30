"""Verify the shipped Keyescape seed against a truly empty data directory."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

from engines.keyescape_engine import KeyescapeEngine
from engines.keyescape_schedule_cache import merge_bundled_slot_templates
from pengucro.storage import load_json


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pengucro-keyescape-seed-") as folder:
        os.environ["PENGUCRO_DATA_DIR"] = folder
        result = merge_bundled_slot_templates()
        cache = load_json("keyescape_slot_templates.json", {"entries": {}})
        entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
        engine = KeyescapeEngine(lambda *_args: None)
        armed = None
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
                if source_day.weekday() < 4:
                    continue
                target_day = source_day + timedelta(days=7)
                for stamp in row.get("slots", {}):
                    slot_id, sources = engine._trusted_slot_from_cache(
                        target_day.isoformat(), stamp, branch_id, theme_num
                    )
                    if slot_id:
                        armed = (
                            branch_id, theme_num, target_day.isoformat(), stamp,
                            slot_id, sources,
                        )
                        break
                if armed:
                    break
            if armed:
                break

        print(f"temporary_data_dir={folder}")
        print(f"seed_available={result.available}")
        print(f"seed_imported={result.imported}")
        print(f"seed_rejected={result.rejected}")
        print(f"cache_keys={len(entries) if isinstance(entries, dict) else 0}")
        print(f"fast_path_example={armed}")
        return 0 if result.imported and armed and not result.rejected else 1


if __name__ == "__main__":
    raise SystemExit(main())
