from datetime import date

from engines.keyescape_engine import KeyescapeEngine
from engines.keyescape_timetable_collector import (
    KeyescapeAutoCollectionLease,
    KeyescapeThemeTarget,
    KeyescapeTimetableCollector,
)


def _rows(gubun):
    return [
        {"num": "7001", "hh": "13", "mm": "35", "enable": "N", "gubun": gubun},
        {"num": "7002", "hh": "14", "mm": "30", "enable": "Y", "gubun": gubun},
    ]


def test_candidate_dates_collect_two_a_days_and_each_weekend_group():
    assert KeyescapeTimetableCollector.candidate_dates(
        date(2026, 8, 23), 7
    ) == [
        date(2026, 8, 29),
        date(2026, 8, 28),
        date(2026, 8, 27),
        date(2026, 8, 26),
        date(2026, 8, 23),
    ]


def test_collector_saves_sold_out_rows_for_future_same_weekday_fast_path(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    collector = KeyescapeTimetableCollector(max_workers=1)
    target = KeyescapeThemeTarget("23", "후즈데어", "71", "63", "AYAKO", 7)
    monkeypatch.setattr(collector, "_discover_catalog", lambda: (1, [target]))
    monkeypatch.setattr(collector, "_server_day", lambda _target: date(2026, 8, 23))
    monkeypatch.setattr(
        collector,
        "_fetch_slots",
        lambda _target, source_day: _rows(
            ("A", "A", "A", "A", "B", "C", "D")[source_day.weekday()]
        ),
    )
    progress = []

    result = collector.collect(progress.append)

    assert result.branch_count == 1
    assert result.theme_count == 1
    assert result.request_count == 5
    assert result.saved_count == 5
    assert result.unavailable_count == 0
    assert result.failed_count == 0
    assert result.coverage == {"A": 1, "B": 1, "C": 1, "D": 1}
    assert progress[-1].completed == progress[-1].total == 5

    engine = KeyescapeEngine(lambda *_args: None)
    assert engine._trusted_slot_from_cache(
        "2026-09-05", "13:35", "23", "71"
    ) == ("7001", ("2026-08-29",))


def test_collector_reports_unpublished_rows_without_treating_them_as_errors(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    collector = KeyescapeTimetableCollector(max_workers=1)
    target = KeyescapeThemeTarget("23", "후즈데어", "71", "63", "AYAKO", 1)
    monkeypatch.setattr(collector, "_discover_catalog", lambda: (1, [target]))
    monkeypatch.setattr(collector, "_server_day", lambda _target: date(2026, 8, 23))
    monkeypatch.setattr(collector, "_fetch_slots", lambda *_args: None)

    result = collector.collect()

    assert result.request_count == 1
    assert result.saved_count == 0
    assert result.unavailable_count == 1
    assert result.failed_count == 0


def test_auto_collection_lease_allows_only_one_process_and_records_success(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    first = KeyescapeAutoCollectionLease(interval_seconds=3600)
    second = KeyescapeAutoCollectionLease(interval_seconds=3600)

    assert first.acquire() is True
    assert second.acquire() is False

    first.release(success=True)
    assert second.acquire() is False


def test_cancelled_auto_collection_stops_before_public_requests(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    collector = KeyescapeTimetableCollector(max_workers=1)
    cancelled = __import__("threading").Event()
    cancelled.set()
    monkeypatch.setattr(
        collector,
        "_discover_catalog",
        lambda _cancel: (12, []),
    )

    result = collector.collect(cancel_event=cancelled)

    assert result.cancelled is True
    assert result.request_count == 0
