import json
from datetime import datetime, timedelta

from engines.keyescape_engine import KST, KeyescapeEngine
from engines.keyescape_schedule_cache import (
    build_bundled_seed_payload,
    merge_bundled_slot_templates,
    remember_slot_template,
)
from pengucro.storage import data_path, load_json


def _saturday_rows():
    return [
        {"num": "9101", "hh": "12", "mm": "00", "enable": "N", "gubun": "C"},
        {"num": "9102", "hh": "13", "mm": "25", "enable": "Y", "gubun": "C"},
    ]


def test_bundled_seed_fills_a_completely_empty_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    today = datetime.now(KST).date()
    source_day = today - timedelta(days=(today.weekday() - 5) % 7 or 7)
    target_day = source_day + timedelta(days=7)
    assert remember_slot_template(
        "https://www.keyescape.com", source_day.isoformat(), "18", "59",
        _saturday_rows(),
    )
    seed = build_bundled_seed_payload(
        load_json("keyescape_slot_templates.json", {}), reference_day=today
    )
    data_path("keyescape_slot_templates.json").unlink()
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

    merged = merge_bundled_slot_templates(seed_path)

    assert merged.available is True
    assert merged.imported == 1
    engine = KeyescapeEngine(lambda *_args: None)
    assert engine._trusted_slot_from_cache(
        target_day.isoformat(), "13:25", "18", "59"
    ) == ("9102", (source_day.isoformat(),))


def test_bundled_seed_rejects_changed_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(
        json.dumps({"version": 1, "entries": {"changed": []}, "payload_sha256": "bad"}),
        encoding="utf-8",
    )

    merged = merge_bundled_slot_templates(seed_path)

    assert merged.available is True
    assert merged.imported == 0
    assert merged.rejected == 1
