"""Build the public Keyescape seed bundled with a Pengucro release."""

from __future__ import annotations

import json
from pathlib import Path

from engines.keyescape_schedule_cache import build_bundled_seed_payload
from pengucro.storage import load_json


def main() -> int:
    cache = load_json("keyescape_slot_templates.json", {"entries": {}})
    seed = build_bundled_seed_payload(cache)
    destination = Path(__file__).resolve().parents[1] / "keyescape_slot_templates_seed.json"
    destination.write_text(
        json.dumps(seed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    entries = seed.get("entries", {})
    rows = sum(len(value) for value in entries.values())
    print(f"seed_path={destination}")
    print(f"seed_themes={len(entries)}")
    print(f"seed_rows={rows}")
    print(f"payload_sha256={seed.get('payload_sha256', '')}")
    return 0 if entries and rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
