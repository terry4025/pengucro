from __future__ import annotations

import copy
import hashlib
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import requests

from pengucro.storage import data_path, load_json, save_json


CATALOG_SCHEMA_VERSION = 1
CATALOG_FILENAME = "site_catalog.json"
CATALOG_BACKUP_FILENAME = "site_catalog.backup.json"
DEFAULT_TTL_HOURS = 12
CATALOG_DISCOVERY_ATTEMPTS = 2
CATALOG_RETRY_DELAY_SECONDS = 0.6


def is_transient_network_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        return response is None or response.status_code == 429 or response.status_code >= 500
    message = str(exc).casefold()
    return any(
        token in message
        for token in (
            "nameresolutionerror",
            "getaddrinfo failed",
            "temporary failure in name resolution",
            "max retries exceeded",
            "connection refused",
            "connection reset",
            "connect timeout",
            "read timed out",
            "사이트 연결 오류",
        )
    )


def friendly_network_error(exc: Exception) -> str:
    message = str(exc).casefold()
    if "nameresolutionerror" in message or "getaddrinfo failed" in message or "name resolution" in message:
        reason = "도메인 주소를 일시적으로 찾지 못했습니다."
    elif isinstance(exc, requests.exceptions.Timeout) or "timed out" in message or "timeout" in message:
        reason = "사이트 응답 시간이 초과되었습니다."
    else:
        reason = "사이트에 일시적으로 연결하지 못했습니다."
    return f"{reason} 기존 정상 카탈로그를 계속 사용합니다."


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_name(value: str) -> str:
    return "".join(value.casefold().split())


def custom_catalog_key(url: str) -> str:
    digest = hashlib.sha256(url.strip().casefold().encode("utf-8")).hexdigest()[:16]
    return f"custom:{digest}"


@dataclass
class DetectionResult:
    engine_id: str
    confidence: int
    evidence: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class CatalogTheme:
    id: str
    name: str
    booking_value: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CatalogTheme":
        return cls(
            id=str(value.get("id", "")),
            name=str(value.get("name", "")),
            booking_value=str(value.get("booking_value", value.get("id", ""))),
            metadata=dict(value.get("metadata", {})),
        )


@dataclass
class CatalogBranch:
    id: str
    name: str
    booking_value: str
    metadata: dict[str, Any] = field(default_factory=dict)
    themes: dict[str, CatalogTheme] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CatalogBranch":
        themes = {
            str(key): CatalogTheme.from_dict(item)
            for key, item in dict(value.get("themes", {})).items()
            if isinstance(item, dict)
        }
        return cls(
            id=str(value.get("id", "")),
            name=str(value.get("name", "")),
            booking_value=str(value.get("booking_value", value.get("id", ""))),
            metadata=dict(value.get("metadata", {})),
            themes=themes,
        )


@dataclass
class SiteCatalog:
    site_key: str
    name: str
    engine_id: str
    url: str
    branches: dict[str, CatalogBranch]
    metadata: dict[str, Any] = field(default_factory=dict)
    last_checked_at: str = ""
    last_success_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SiteCatalog":
        branches = {
            str(key): CatalogBranch.from_dict(item)
            for key, item in dict(value.get("branches", {})).items()
            if isinstance(item, dict)
        }
        return cls(
            site_key=str(value.get("site_key", "")),
            name=str(value.get("name", "")),
            engine_id=str(value.get("engine_id", "")),
            url=str(value.get("url", "")),
            branches=branches,
            metadata=dict(value.get("metadata", {})),
            last_checked_at=str(value.get("last_checked_at", "")),
            last_success_at=str(value.get("last_success_at", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def item_count(self) -> int:
        return len(self.branches) + sum(len(branch.themes) for branch in self.branches.values())


@dataclass
class CatalogChange:
    kind: str
    entity: str
    parent_id: str = ""
    old_id: str = ""
    new_id: str = ""
    old_name: str = ""
    new_name: str = ""
    new_data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CatalogChange":
        allowed = {key: value.get(key, "") for key in cls.__dataclass_fields__}
        allowed["new_data"] = dict(value.get("new_data", {}))
        return cls(**allowed)


@dataclass
class RefreshResult:
    site_key: str
    site_name: str
    status: str
    applied_changes: list[CatalogChange] = field(default_factory=list)
    pending_changes: list[CatalogChange] = field(default_factory=list)
    error: str = ""
    checked_at: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.applied_changes or self.pending_changes)


class CatalogProvider(Protocol):
    engine_id: str

    def detect(self, url: str, html: str) -> DetectionResult: ...

    def discover(self, site_config: dict[str, Any], target_date: str) -> SiteCatalog: ...

    def validate(self, candidate: SiteCatalog) -> ValidationResult: ...


class CatalogStore:
    def __init__(self, filename: str = CATALOG_FILENAME) -> None:
        self.filename = filename

    @staticmethod
    def _decode(raw: Any) -> dict[str, SiteCatalog]:
        if not isinstance(raw, dict) or raw.get("schema_version") != CATALOG_SCHEMA_VERSION:
            return {}
        sites = raw.get("sites", {})
        if not isinstance(sites, dict):
            return {}
        decoded: dict[str, SiteCatalog] = {}
        for key, value in sites.items():
            if isinstance(value, dict):
                catalog = SiteCatalog.from_dict(value)
                if catalog.site_key and catalog.branches:
                    decoded[str(key)] = catalog
        return decoded

    def load(self) -> dict[str, SiteCatalog]:
        catalogs = self._decode(load_json(self.filename, {}))
        if catalogs:
            return catalogs
        return self._decode(load_json(CATALOG_BACKUP_FILENAME, {}))

    def save(self, catalogs: dict[str, SiteCatalog]) -> Path:
        current = data_path(self.filename)
        backup = data_path(CATALOG_BACKUP_FILENAME)
        if current.exists():
            try:
                shutil.copy2(current, backup)
            except OSError:
                pass
        payload = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "sites": {key: catalog.to_dict() for key, catalog in catalogs.items()},
        }
        return save_json(self.filename, payload)


class CatalogService:
    def __init__(self, providers: dict[str, CatalogProvider], store: CatalogStore | None = None) -> None:
        self.providers = providers
        self.store = store or CatalogStore()
        self.catalogs = self.store.load()

    def seed_fallback(self, catalog: SiteCatalog) -> None:
        """Keep bundled data as an in-memory baseline without overwriting a valid cache."""
        self.catalogs.setdefault(catalog.site_key, copy.deepcopy(catalog))

    def is_stale(self, site_key: str, ttl_hours: int = DEFAULT_TTL_HOURS) -> bool:
        catalog = self.catalogs.get(site_key)
        if not catalog or not catalog.last_success_at:
            return True
        try:
            checked = datetime.fromisoformat(catalog.last_success_at)
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - checked >= timedelta(hours=ttl_hours)
        except ValueError:
            return True

    @staticmethod
    def validate_catalog(candidate: SiteCatalog) -> ValidationResult:
        errors: list[str] = []
        if not candidate.site_key or not candidate.engine_id or not candidate.url:
            errors.append("사이트 식별 정보가 없습니다.")
        if not candidate.branches:
            errors.append("지점 정보가 비어 있습니다.")
        seen_branch_values: set[str] = set()
        for branch in candidate.branches.values():
            if not branch.id or not branch.name or not branch.booking_value:
                errors.append("필수 지점 정보가 누락되었습니다.")
            identity = f"{branch.metadata.get('subject', '')}:{branch.booking_value}"
            if identity in seen_branch_values:
                errors.append(f"중복 지점 ID가 있습니다: {identity}")
            seen_branch_values.add(identity)
            if not branch.themes:
                errors.append(f"'{branch.name}' 지점의 테마가 비어 있습니다.")
            seen_theme_values: set[str] = set()
            for theme in branch.themes.values():
                if not theme.id or not theme.name or not theme.booking_value:
                    errors.append(f"'{branch.name}' 지점의 필수 테마 정보가 누락되었습니다.")
                if theme.booking_value in seen_theme_values:
                    errors.append(f"'{branch.name}' 지점에 중복 테마 ID가 있습니다: {theme.booking_value}")
                seen_theme_values.add(theme.booking_value)
        return ValidationResult(not errors, errors)

    @staticmethod
    def _find_id_changes(
        old_items: dict[str, Any], new_items: dict[str, Any]
    ) -> list[tuple[Any, Any]]:
        added = [item for key, item in new_items.items() if key not in old_items]
        matches: list[tuple[Any, Any]] = []
        used_old: set[str] = set()
        used_new: set[str] = set()
        for new in added:
            for old in old_items.values():
                if old.id in used_old or new.id in used_new:
                    continue
                if normalize_name(old.name) != normalize_name(new.name):
                    continue
                matches.append((old, new))
                used_old.add(old.id)
                used_new.add(new.id)
                break
        return matches

    def _merge_safe(
        self, current: SiteCatalog | None, candidate: SiteCatalog
    ) -> tuple[SiteCatalog, list[CatalogChange], list[CatalogChange], str]:
        if current is None:
            applied: list[CatalogChange] = []
            for branch in candidate.branches.values():
                applied.append(CatalogChange("added", "branch", new_id=branch.id, new_name=branch.name))
                for theme in branch.themes.values():
                    applied.append(
                        CatalogChange(
                            "added", "theme", parent_id=branch.id, new_id=theme.id, new_name=theme.name
                        )
                    )
            return copy.deepcopy(candidate), applied, [], ""

        current_count = current.item_count()
        candidate_count = candidate.item_count()
        if current_count and candidate_count <= current_count * 0.5:
            return current, [], [], "기존 항목의 50% 이상이 사라져 후보 데이터를 거부했습니다."

        merged = copy.deepcopy(candidate)
        applied: list[CatalogChange] = []
        pending: list[CatalogChange] = []

        branch_id_changes = self._find_id_changes(current.branches, candidate.branches)
        changed_new_branch_ids = {new.id for _, new in branch_id_changes}
        changed_old_branch_ids = {old.id for old, _ in branch_id_changes}

        for old, new in branch_id_changes:
            pending.append(
                CatalogChange(
                    "id_changed",
                    "branch",
                    old_id=old.id,
                    new_id=new.id,
                    old_name=old.name,
                    new_name=new.name,
                    new_data=asdict(new),
                )
            )
            merged.branches.pop(new.id, None)
            merged.branches[old.id] = copy.deepcopy(old)

        for branch_id, old_branch in current.branches.items():
            if branch_id not in candidate.branches:
                if branch_id in changed_old_branch_ids:
                    continue
                merged.branches[branch_id] = copy.deepcopy(old_branch)
                pending.append(
                    CatalogChange(
                        "removed", "branch", old_id=branch_id, old_name=old_branch.name
                    )
                )
                continue

            new_branch = merged.branches[branch_id]
            if old_branch.name != new_branch.name:
                applied.append(
                    CatalogChange(
                        "renamed",
                        "branch",
                        old_id=branch_id,
                        new_id=branch_id,
                        old_name=old_branch.name,
                        new_name=new_branch.name,
                    )
                )

            theme_id_changes = self._find_id_changes(old_branch.themes, new_branch.themes)
            changed_new_theme_ids = {new.id for _, new in theme_id_changes}
            changed_old_theme_ids = {old.id for old, _ in theme_id_changes}
            for old, new in theme_id_changes:
                pending.append(
                    CatalogChange(
                        "id_changed",
                        "theme",
                        parent_id=branch_id,
                        old_id=old.id,
                        new_id=new.id,
                        old_name=old.name,
                        new_name=new.name,
                        new_data=asdict(new),
                    )
                )
                new_branch.themes.pop(new.id, None)
                new_branch.themes[old.id] = copy.deepcopy(old)
            for theme_id, old_theme in old_branch.themes.items():
                if theme_id not in candidate.branches[branch_id].themes:
                    if theme_id in changed_old_theme_ids:
                        continue
                    new_branch.themes[theme_id] = copy.deepcopy(old_theme)
                    pending.append(
                        CatalogChange(
                            "removed",
                            "theme",
                            parent_id=branch_id,
                            old_id=theme_id,
                            old_name=old_theme.name,
                        )
                    )
                else:
                    current_theme = candidate.branches[branch_id].themes[theme_id]
                    if old_theme.name != current_theme.name:
                        applied.append(
                            CatalogChange(
                                "renamed",
                                "theme",
                                parent_id=branch_id,
                                old_id=theme_id,
                                new_id=theme_id,
                                old_name=old_theme.name,
                                new_name=current_theme.name,
                            )
                        )
            for theme_id, theme in candidate.branches[branch_id].themes.items():
                if theme_id not in old_branch.themes and theme_id not in changed_new_theme_ids:
                    applied.append(
                        CatalogChange(
                            "added",
                            "theme",
                            parent_id=branch_id,
                            new_id=theme_id,
                            new_name=theme.name,
                        )
                    )

        for branch_id, branch in candidate.branches.items():
            if branch_id not in current.branches and branch_id not in changed_new_branch_ids:
                applied.append(CatalogChange("added", "branch", new_id=branch_id, new_name=branch.name))
                for theme in branch.themes.values():
                    applied.append(
                        CatalogChange(
                            "added", "theme", parent_id=branch_id, new_id=theme.id, new_name=theme.name
                        )
                    )

        merged.metadata["pending_changes"] = [asdict(change) for change in pending]
        return merged, applied, pending, ""

    def refresh(
        self,
        site_config: dict[str, Any],
        target_date: str,
        *,
        force: bool = False,
    ) -> RefreshResult:
        site_key = str(site_config.get("catalog_key", ""))
        site_name = str(site_config.get("name", site_key))
        checked_at = utc_now_iso()
        if not force and not self.is_stale(site_key):
            return RefreshResult(site_key, site_name, "fresh", checked_at=checked_at)
        engine_id = str(site_config.get("engine_id", ""))
        provider = self.providers.get(engine_id)
        if not provider:
            return RefreshResult(
                site_key,
                site_name,
                "error",
                error=f"카탈로그 제공자를 찾을 수 없습니다: {engine_id}",
                checked_at=checked_at,
            )
        try:
            candidate = None
            for attempt in range(CATALOG_DISCOVERY_ATTEMPTS):
                try:
                    candidate = provider.discover(site_config, target_date)
                    break
                except Exception as exc:
                    if not is_transient_network_error(exc):
                        raise
                    if attempt + 1 >= CATALOG_DISCOVERY_ATTEMPTS:
                        return RefreshResult(
                            site_key,
                            site_name,
                            "deferred",
                            error=friendly_network_error(exc),
                            checked_at=checked_at,
                        )
                    time.sleep(CATALOG_RETRY_DELAY_SECONDS)
            if candidate is None:
                return RefreshResult(
                    site_key,
                    site_name,
                    "deferred",
                    error="사이트에 일시적으로 연결하지 못했습니다. 기존 정상 카탈로그를 계속 사용합니다.",
                    checked_at=checked_at,
                )
            candidate.last_checked_at = checked_at
            validation = self.validate_catalog(candidate)
            provider_validation = provider.validate(candidate)
            errors = validation.errors + provider_validation.errors
            if errors:
                return RefreshResult(
                    site_key,
                    site_name,
                    "rejected",
                    error=f"사이트 구조 변경 가능성: {' '.join(errors)}",
                    checked_at=checked_at,
                )
            merged, applied, pending, merge_error = self._merge_safe(self.catalogs.get(site_key), candidate)
            if merge_error:
                return RefreshResult(
                    site_key,
                    site_name,
                    "rejected",
                    error=f"사이트 구조 변경 가능성: {merge_error}",
                    checked_at=checked_at,
                )
            merged.last_checked_at = checked_at
            merged.last_success_at = checked_at
            self.catalogs[site_key] = merged
            self.store.save(self.catalogs)
            status = "changed" if applied or pending else "unchanged"
            return RefreshResult(site_key, site_name, status, applied, pending, checked_at=checked_at)
        except Exception as exc:
            return RefreshResult(site_key, site_name, "error", error=str(exc), checked_at=checked_at)

    def apply_pending(self, site_key: str, selected: list[CatalogChange]) -> SiteCatalog | None:
        catalog = self.catalogs.get(site_key)
        if not catalog:
            return None
        for change in selected:
            if change.entity == "branch":
                if change.kind == "removed":
                    catalog.branches.pop(change.old_id, None)
                elif change.kind == "id_changed" and change.new_data:
                    catalog.branches.pop(change.old_id, None)
                    branch = CatalogBranch.from_dict(change.new_data)
                    catalog.branches[branch.id] = branch
            elif change.entity == "theme":
                branch = catalog.branches.get(change.parent_id)
                if not branch:
                    continue
                if change.kind == "removed":
                    branch.themes.pop(change.old_id, None)
                elif change.kind == "id_changed" and change.new_data:
                    branch.themes.pop(change.old_id, None)
                    theme = CatalogTheme.from_dict(change.new_data)
                    branch.themes[theme.id] = theme
        selected_keys = {
            (item.kind, item.entity, item.parent_id, item.old_id, item.new_id) for item in selected
        }
        remaining = []
        for raw in catalog.metadata.get("pending_changes", []):
            change = CatalogChange.from_dict(raw)
            key = (change.kind, change.entity, change.parent_id, change.old_id, change.new_id)
            if key not in selected_keys:
                remaining.append(raw)
        catalog.metadata["pending_changes"] = remaining
        self.store.save(self.catalogs)
        return catalog

    def pending_changes(self, site_key: str) -> list[CatalogChange]:
        catalog = self.catalogs.get(site_key)
        if not catalog:
            return []
        return [CatalogChange.from_dict(item) for item in catalog.metadata.get("pending_changes", [])]
