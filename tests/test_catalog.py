from __future__ import annotations

from copy import deepcopy

import requests

from pengucro.catalog import (
    CatalogBranch,
    CatalogChange,
    CatalogService,
    CatalogStore,
    CatalogTheme,
    DetectionResult,
    SiteCatalog,
    ValidationResult,
    utc_now_iso,
)


def make_catalog(*, theme_name="기존 테마", include_removed=True, extra_count=0):
    themes = {"10": CatalogTheme("10", theme_name, "10")}
    if include_removed:
        themes["11"] = CatalogTheme("11", "삭제 후보", "11")
    for index in range(extra_count):
        theme_id = str(100 + index)
        themes[theme_id] = CatalogTheme(theme_id, f"추가 {index}", theme_id)
    return SiteCatalog(
        site_key="test:site",
        name="테스트",
        engine_id="fake",
        url="https://example.com",
        branches={"1": CatalogBranch("1", "본점", "1", themes=themes)},
    )


class FakeProvider:
    engine_id = "fake"

    def __init__(self, candidate):
        self.candidate = candidate

    def detect(self, url, html):
        return DetectionResult(self.engine_id, 100, ["test"])

    def discover(self, site_config, target_date):
        return deepcopy(self.candidate)

    def validate(self, candidate):
        return ValidationResult(True, [])


def make_service(tmp_path, monkeypatch, current, candidate):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    service = CatalogService({"fake": FakeProvider(candidate)}, CatalogStore())
    service.catalogs[current.site_key] = current
    return service


def test_safe_merge_applies_add_and_rename_but_holds_removal(tmp_path, monkeypatch):
    current = make_catalog()
    candidate = make_catalog(theme_name="변경된 테마", include_removed=False, extra_count=1)
    service = make_service(tmp_path, monkeypatch, current, candidate)

    result = service.refresh(
        {"catalog_key": "test:site", "name": "테스트", "engine_id": "fake"},
        "2026-08-01",
        force=True,
    )

    assert result.status == "changed"
    assert {(change.kind, change.new_name) for change in result.applied_changes} >= {
        ("renamed", "변경된 테마"),
        ("added", "추가 0"),
    }
    assert [(change.kind, change.old_id) for change in result.pending_changes] == [("removed", "11")]
    assert service.catalogs["test:site"].branches["1"].themes["11"].name == "삭제 후보"


def test_same_name_with_new_id_is_held_for_review(tmp_path, monkeypatch):
    current = make_catalog(include_removed=False)
    candidate = make_catalog(include_removed=False)
    old = candidate.branches["1"].themes.pop("10")
    candidate.branches["1"].themes["99"] = CatalogTheme("99", old.name, "99")
    service = make_service(tmp_path, monkeypatch, current, candidate)

    result = service.refresh(
        {"catalog_key": "test:site", "name": "테스트", "engine_id": "fake"},
        "2026-08-01",
        force=True,
    )

    assert [(change.kind, change.old_id, change.new_id) for change in result.pending_changes] == [
        ("id_changed", "10", "99")
    ]
    themes = service.catalogs["test:site"].branches["1"].themes
    assert "10" in themes and "99" not in themes


def test_same_name_with_extra_id_is_held_even_when_old_id_remains(tmp_path, monkeypatch):
    current = make_catalog(include_removed=False)
    candidate = make_catalog(include_removed=False)
    candidate.branches["1"].themes["99"] = CatalogTheme("99", "기존 테마", "99")
    service = make_service(tmp_path, monkeypatch, current, candidate)

    result = service.refresh(
        {"catalog_key": "test:site", "name": "테스트", "engine_id": "fake"},
        "2026-08-01",
        force=True,
    )

    assert [(change.kind, change.old_id, change.new_id) for change in result.pending_changes] == [
        ("id_changed", "10", "99")
    ]
    assert "99" not in service.catalogs["test:site"].branches["1"].themes


def test_mass_disappearance_rejects_candidate_and_preserves_current(tmp_path, monkeypatch):
    current = make_catalog(extra_count=5)
    candidate = make_catalog(include_removed=False)
    service = make_service(tmp_path, monkeypatch, current, candidate)

    result = service.refresh(
        {"catalog_key": "test:site", "name": "테스트", "engine_id": "fake"},
        "2026-08-01",
        force=True,
    )

    assert result.status == "rejected"
    assert "50%" in result.error
    assert service.catalogs["test:site"].item_count() == current.item_count()


def test_selected_pending_removal_is_applied_atomically(tmp_path, monkeypatch):
    current = make_catalog()
    candidate = make_catalog(include_removed=False, extra_count=1)
    service = make_service(tmp_path, monkeypatch, current, candidate)
    result = service.refresh(
        {"catalog_key": "test:site", "name": "테스트", "engine_id": "fake"},
        "2026-08-01",
        force=True,
    )

    service.apply_pending("test:site", result.pending_changes)

    assert "11" not in service.catalogs["test:site"].branches["1"].themes
    assert service.pending_changes("test:site") == []


def test_store_uses_backup_when_primary_is_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    store = CatalogStore()
    catalog = make_catalog()
    catalog.last_success_at = utc_now_iso()
    store.save({catalog.site_key: catalog})
    store.save({catalog.site_key: catalog})
    (tmp_path / "site_catalog.json").write_text("not json", encoding="utf-8")

    loaded = store.load()

    assert loaded["test:site"].branches["1"].themes["10"].name == "기존 테마"


def test_ttl_distinguishes_fresh_and_missing_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    service = CatalogService({})
    catalog = make_catalog()
    catalog.last_success_at = utc_now_iso()
    service.catalogs[catalog.site_key] = catalog

    assert not service.is_stale(catalog.site_key)
    assert service.is_stale("missing")


class FlakyProvider(FakeProvider):
    def __init__(self, candidate, failures):
        super().__init__(candidate)
        self.failures = failures
        self.calls = 0

    def discover(self, site_config, target_date):
        self.calls += 1
        if self.calls <= self.failures:
            raise requests.exceptions.ConnectionError(
                "NameResolutionError: getaddrinfo failed"
            )
        return deepcopy(self.candidate)


def test_transient_dns_failure_retries_then_refreshes(tmp_path, monkeypatch):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("pengucro.catalog.time.sleep", lambda _seconds: None)
    current = make_catalog(include_removed=False)
    candidate = make_catalog(include_removed=False, extra_count=1)
    provider = FlakyProvider(candidate, failures=1)
    service = CatalogService({"fake": provider}, CatalogStore())
    service.catalogs[current.site_key] = current

    result = service.refresh(
        {"catalog_key": "test:site", "name": "테스트", "engine_id": "fake"},
        "2026-08-01",
        force=True,
    )

    assert provider.calls == 2
    assert result.status == "changed"
    assert "100" in service.catalogs["test:site"].branches["1"].themes


def test_repeated_dns_failure_defers_and_preserves_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("pengucro.catalog.time.sleep", lambda _seconds: None)
    current = make_catalog(include_removed=False)
    provider = FlakyProvider(make_catalog(include_removed=False, extra_count=1), failures=2)
    service = CatalogService({"fake": provider}, CatalogStore())
    service.catalogs[current.site_key] = current

    result = service.refresh(
        {"catalog_key": "test:site", "name": "테스트", "engine_id": "fake"},
        "2026-08-01",
        force=True,
    )

    assert provider.calls == 2
    assert result.status == "deferred"
    assert "기존 정상 카탈로그" in result.error
    assert "100" not in service.catalogs["test:site"].branches["1"].themes
