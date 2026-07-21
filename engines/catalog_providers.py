from __future__ import annotations

import copy
import re
import time
import urllib.parse
from datetime import date
from typing import Any

import requests
from bs4 import BeautifulSoup

from engines.site_parser import normalize_naver_url, parse_booking_site
from engines.zeroworld_catalog import decode_body, parse_theme_list
from pengucro.catalog import (
    CatalogBranch,
    CatalogService,
    CatalogTheme,
    DetectionResult,
    SiteCatalog,
    ValidationResult,
    custom_catalog_key,
    utc_now_iso,
)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
KNOWN_ENGINES = {
    "zeroworldkorea.com": "sinbiworld",
    "doomescape.com": "doomescape",
    "www.xn--2e0b040a4xj.com": "jigubyeol",
    "xn--2e0b040a4xj.com": "jigubyeol",
    "www.keyescape.com": "keyescape",
    "keyescape.com": "keyescape",
    "booking.naver.com": "naver",
    "m.booking.naver.com": "naver",
}


def _base_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


def _site_catalog(
    site_config: dict[str, Any],
    engine_id: str,
    branches: dict[str, CatalogBranch],
    metadata: dict[str, Any] | None = None,
) -> SiteCatalog:
    return SiteCatalog(
        site_key=str(site_config["catalog_key"]),
        name=str(site_config.get("name", site_config["catalog_key"])),
        engine_id=engine_id,
        url=str(site_config.get("url", "")),
        branches=branches,
        metadata=metadata or {},
    )


def _legacy_result_to_catalog(
    site_config: dict[str, Any], result: dict[str, Any], engine_id: str
) -> SiteCatalog:
    branches: dict[str, CatalogBranch] = {}
    themes_by_branch = result.get("themes", {})
    for branch_name, booking_value in result.get("branches", {}).items():
        branch_id = str(booking_value)
        themes: dict[str, CatalogTheme] = {}
        for theme_name, raw_value in themes_by_branch.get(branch_id, {}).items():
            if isinstance(raw_value, dict):
                booking_id = str(raw_value.get("info_num", raw_value.get("id", "")))
                metadata = dict(raw_value)
            else:
                booking_id = str(raw_value)
                metadata = {}
            if booking_id:
                themes[booking_id] = CatalogTheme(booking_id, str(theme_name), booking_id, metadata)
        branches[branch_id] = CatalogBranch(branch_id, str(branch_name), branch_id, themes=themes)
    return _site_catalog(
        site_config,
        engine_id,
        branches,
        {
            "base_url": result.get("base_url", _base_url(site_config.get("url", ""))),
            "engine_options": dict(site_config.get("engine_options", {})),
        },
    )


class BaseProvider:
    engine_id = ""

    def validate(self, candidate: SiteCatalog) -> ValidationResult:
        if candidate.engine_id != self.engine_id:
            return ValidationResult(False, ["엔진 식별자가 일치하지 않습니다."])
        return ValidationResult(True, [])


class SinbiWorldProvider(BaseProvider):
    engine_id = "sinbiworld"

    def detect(self, url: str, html: str) -> DetectionResult:
        host = urllib.parse.urlparse(url).netloc.casefold()
        evidence = []
        score = 0
        if KNOWN_ENGINES.get(host) == self.engine_id:
            score = 100
            evidence.append("공식 제로월드 도메인")
        for token, points, label in (
            ("rev.make.sel.php", 35, "테마 AJAX 엔드포인트"),
            ("fun_theme_time_list", 25, "시간 조회 함수"),
            ("s_subj", 20, "지점군 매개변수"),
            ("zizum_num", 20, "지점 매개변수"),
        ):
            if token in html:
                score = min(100, score + points)
                evidence.append(label)
        return DetectionResult(self.engine_id, score, evidence)

    def discover(self, site_config: dict[str, Any], target_date: str) -> SiteCatalog:
        base = str(site_config.get("base_url") or _base_url(site_config["url"]))
        select_url = urllib.parse.urljoin(base, "/core/res/rev.make.sel.php")
        home_url = urllib.parse.urljoin(base, "/layout/res/home.php")
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        discovered: dict[str, tuple[str, str, str]] = {}
        for subject in ("A", "B"):
            response = session.get(
                home_url,
                params={"go": "rev.make", "s_subj": subject},
                timeout=10,
            )
            response.raise_for_status()
            soup = BeautifulSoup(decode_body(response.content), "html.parser")
            for anchor in soup.find_all("a", href=True):
                parsed = urllib.parse.urlparse(anchor["href"])
                query = urllib.parse.parse_qs(parsed.query)
                branch_id = query.get("zizum_num", [""])[0]
                link_subject = query.get("s_subj", [subject])[0]
                if not branch_id:
                    continue
                name_node = anchor.select_one(".rese-spot__text")
                if name_node:
                    name = next(name_node.stripped_strings, "")
                else:
                    name = anchor.get_text(" ", strip=True).split("서울")[0].strip()
                bracket_name = re.search(r"\[([^\]]+)\]", name)
                if bracket_name:
                    name = re.sub(r"\s+", "", bracket_name.group(1))
                else:
                    name = re.sub(r"^제로월드\s+", "", name).strip()
                if name:
                    discovered[f"{link_subject}:{branch_id}"] = (name, branch_id, link_subject)
        if not discovered:
            options = site_config.get("engine_options", {})
            subjects = options.get("subject_by_branch", {})
            for name, branch_id in site_config.get("branches", {}).items():
                branch_id = str(branch_id)
                subject = str(subjects.get(branch_id, "A"))
                discovered[f"{subject}:{branch_id}"] = (str(name), branch_id, subject)

        branches: dict[str, CatalogBranch] = {}
        for index, (stable_id, (name, branch_id, subject)) in enumerate(discovered.items()):
            response = session.post(
                select_url,
                data={
                    "act": "theme_list",
                    "zizum_num": branch_id,
                    "rev_days": target_date,
                    "theme_num": "",
                    "s_subj": subject,
                },
                headers={"Referer": f"{home_url}?go=rev.make&s_subj={subject}&zizum_num={branch_id}"},
                timeout=10,
            )
            response.raise_for_status()
            theme_map = parse_theme_list(decode_body(response.content))
            themes = {
                str(theme_id): CatalogTheme(str(theme_id), theme_name, str(theme_id))
                for theme_name, theme_id in theme_map.items()
            }
            branches[stable_id] = CatalogBranch(
                stable_id,
                name,
                branch_id,
                metadata={"subject": subject},
                themes=themes,
            )
            if index + 1 < len(discovered):
                time.sleep(0.25)
        subject_by_branch = {
            branch.booking_value: str(branch.metadata.get("subject", "A"))
            for branch in branches.values()
        }
        return _site_catalog(
            site_config,
            self.engine_id,
            branches,
            {
                "base_url": base,
                "engine_options": {
                    "select_url": select_url,
                    "action_url": urllib.parse.urljoin(base, "/core/res/rev.act.php"),
                    "payment_url": urllib.parse.urljoin(base, "/core/res/rev.make.mutong.php"),
                    "home_url": home_url,
                    "subject_by_branch": subject_by_branch,
                },
            },
        )


class JigubyeolProvider(BaseProvider):
    engine_id = "jigubyeol"

    def detect(self, url: str, html: str) -> DetectionResult:
        host = urllib.parse.urlparse(url).netloc.casefold()
        evidence = []
        score = 100 if KNOWN_ENGINES.get(host) == self.engine_id else 0
        if score:
            evidence.append("공식 지구별 도메인")
        for token, points, label in (
            ("reservation/create", 40, "Laravel 예약 생성 경로"),
            ('name="csrf-token"', 20, "CSRF 토큰"),
            ("branch=", 20, "지점 매개변수"),
            ("theme=", 20, "테마 매개변수"),
        ):
            if token in html:
                score = min(100, score + points)
                evidence.append(label)
        return DetectionResult(self.engine_id, score, evidence)

    def discover(self, site_config: dict[str, Any], target_date: str) -> SiteCatalog:
        result = parse_booking_site(site_config["url"], site_config.get("name", ""))
        if result.get("style") != "jigubyeol":
            raise ValueError("지구별 계열 카탈로그 구조가 아닙니다.")
        catalog = _legacy_result_to_catalog(site_config, result, self.engine_id)
        catalog.metadata["engine_options"] = {"base_url": result["base_url"]}
        return catalog


class KeyescapeProvider(BaseProvider):
    engine_id = "keyescape"

    def detect(self, url: str, html: str) -> DetectionResult:
        host = urllib.parse.urlparse(url).netloc.casefold()
        evidence = []
        score = 100 if KNOWN_ENGINES.get(host) == self.engine_id else 0
        if score:
            evidence.append("공식 키이스케이프 도메인")
        for token, points, label in (
            ("run_proc.php", 35, "공통 처리 API"),
            ("get_theme_info_list", 35, "테마 조회 명령"),
            ('name="zizum"', 15, "지점 선택 필드"),
            ('name="theme"', 15, "테마 선택 필드"),
        ):
            if token in html:
                score = min(100, score + points)
                evidence.append(label)
        return DetectionResult(self.engine_id, score, evidence)

    def discover(self, site_config: dict[str, Any], target_date: str) -> SiteCatalog:
        base = str(site_config.get("base_url") or _base_url(site_config["url"]))
        reservation_url = urllib.parse.urljoin(base, "/reservation.php")
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        response = session.get(reservation_url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        select = soup.find("select", attrs={"name": "zizum"})
        if not select:
            raise ValueError("키이스케이프 지점 목록을 찾지 못했습니다.")
        branches: dict[str, CatalogBranch] = {}
        options = [(item.get_text(" ", strip=True), str(item.get("value", ""))) for item in select.find_all("option")]
        options = [(name, branch_id) for name, branch_id in options if branch_id and name]
        api_url = urllib.parse.urljoin(base, "/controller/run_proc.php")
        for index, (name, branch_id) in enumerate(options):
            result = session.post(
                api_url,
                data={"t": "get_theme_info_list", "zizum_num": branch_id},
                timeout=10,
            )
            result.raise_for_status()
            payload = result.json()
            themes: dict[str, CatalogTheme] = {}
            for item in payload.get("data", []) if payload.get("status") else []:
                info_num = str(item.get("info_num", ""))
                theme_num = str(item.get("level_num", item.get("theme_num", "")))
                theme_name = str(item.get("info_name", "")).strip()
                if info_num and theme_name:
                    themes[info_num] = CatalogTheme(
                        info_num,
                        theme_name,
                        info_num,
                        {
                            "info_num": info_num,
                            "theme_num": theme_num,
                            "doing": item.get("doing", 0),
                        },
                    )
            branches[branch_id] = CatalogBranch(branch_id, name, branch_id, themes=themes)
            if index + 1 < len(options):
                time.sleep(0.25)
        return _site_catalog(
            site_config,
            self.engine_id,
            branches,
            {"base_url": base, "engine_options": {"base_url": base}},
        )


class DoomescapeProvider(BaseProvider):
    engine_id = "doomescape"

    def detect(self, url: str, html: str) -> DetectionResult:
        host = urllib.parse.urlparse(url).netloc.casefold()
        evidence = []
        score = 100 if KNOWN_ENGINES.get(host) == self.engine_id else 0
        if score:
            evidence.append("공식 둠이스케이프 도메인")
        for token, points, label in (
            ('name="s_zizum"', 35, "둠 지점 선택 필드"),
            ("tm_box", 35, "둠 테마 카드"),
            ("rev.make", 15, "예약 페이지"),
            ("rev.act.php", 15, "예약 처리 경로"),
        ):
            if token in html:
                score = min(100, score + points)
                evidence.append(label)
        return DetectionResult(self.engine_id, score, evidence)

    def discover(self, site_config: dict[str, Any], target_date: str) -> SiteCatalog:
        base = str(site_config.get("base_url") or _base_url(site_config["url"]))
        home_url = urllib.parse.urljoin(base, "/layout/res/home.php")
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        response = session.get(home_url, params={"go": "rev.make"}, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(decode_body(response.content), "html.parser")
        select = soup.find("select", attrs={"name": "s_zizum"})
        if not select:
            raise ValueError("둠이스케이프 지점 목록을 찾지 못했습니다.")
        options = [(item.get_text(" ", strip=True), str(item.get("value", ""))) for item in select.find_all("option")]
        options = [(name, branch_id) for name, branch_id in options if name and branch_id]
        branches: dict[str, CatalogBranch] = {}
        for index, (name, branch_id) in enumerate(options):
            page = session.get(
                home_url,
                params={"go": "rev.make", "s_zizum": branch_id, "rev_days": target_date},
                timeout=10,
            )
            page.raise_for_status()
            page_soup = BeautifulSoup(decode_body(page.content), "html.parser")
            themes: dict[str, CatalogTheme] = {}
            for box in page_soup.select(".tm_box"):
                name_node = box.select_one(".info_box .tit .name")
                image = box.select_one(".img_box img")
                theme_name = name_node.get_text(" ", strip=True) if name_node else ""
                id_match = re.search(r"/theme/(\d+)_", image.get("src", "") if image else "")
                if theme_name and id_match:
                    theme_id = id_match.group(1)
                    themes[theme_id] = CatalogTheme(theme_id, theme_name, theme_id)
            branches[branch_id] = CatalogBranch(branch_id, name, branch_id, themes=themes)
            if index + 1 < len(options):
                time.sleep(0.25)
        return _site_catalog(
            site_config,
            self.engine_id,
            branches,
            {
                "base_url": base,
                "engine_options": {
                    "base_url": base,
                    "home_url": home_url,
                    "action_url": urllib.parse.urljoin(base, "/core/res/rev.act.php"),
                },
            },
        )


class NaverProvider(BaseProvider):
    engine_id = "naver"

    def detect(self, url: str, html: str) -> DetectionResult:
        normalized = normalize_naver_url(url)
        return DetectionResult(
            self.engine_id,
            100 if normalized else 0,
            ["네이버 예약 URL"] if normalized else [],
        )

    def discover(self, site_config: dict[str, Any], target_date: str) -> SiteCatalog:
        result = parse_booking_site(site_config["url"], site_config.get("name", ""))
        return _legacy_result_to_catalog(site_config, result, self.engine_id)


class ZeroWorldLaravelProvider(BaseProvider):
    engine_id = "zeroworld_laravel"

    def detect(self, url: str, html: str) -> DetectionResult:
        evidence = []
        score = 0
        for token, points, label in (
            ('name="csrf-token"', 30, "CSRF 토큰"),
            ("/reservation/theme", 35, "테마 JSON API"),
            ("reservationDate", 15, "예약 날짜 필드"),
            ("themePK", 20, "테마 PK 필드"),
        ):
            if token in html:
                score += points
                evidence.append(label)
        return DetectionResult(self.engine_id, min(score, 100), evidence)

    def discover(self, site_config: dict[str, Any], target_date: str) -> SiteCatalog:
        result = parse_booking_site(site_config["url"], site_config.get("name", ""))
        if result.get("style") != "zeroworld":
            raise ValueError("Laravel 제로월드 계열 카탈로그 구조가 아닙니다.")
        catalog = _legacy_result_to_catalog(site_config, result, self.engine_id)
        catalog.metadata["engine_options"] = {
            "base_url": result["base_url"],
            "reservation_url": result["url"],
        }
        return catalog


def default_providers() -> dict[str, BaseProvider]:
    providers: list[BaseProvider] = [
        SinbiWorldProvider(),
        DoomescapeProvider(),
        JigubyeolProvider(),
        KeyescapeProvider(),
        ZeroWorldLaravelProvider(),
        NaverProvider(),
    ]
    return {provider.engine_id: provider for provider in providers}


def rank_engine_candidates(url: str, html: str = "") -> list[DetectionResult]:
    providers = default_providers()
    if not html and not normalize_naver_url(url):
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        response.raise_for_status()
        html = response.text
    return sorted(
        (provider.detect(url, html) for provider in providers.values()),
        key=lambda item: item.confidence,
        reverse=True,
    )


def detect_engine(url: str, html: str = "") -> tuple[DetectionResult, list[DetectionResult]]:
    results = rank_engine_candidates(url, html)
    return results[0], results


def analyze_booking_site(url: str, site_name: str, target_date: str | None = None) -> dict[str, Any]:
    normalized = normalize_naver_url(url)
    source_url = normalized or url
    candidates = rank_engine_candidates(source_url)
    base_config = {
        "name": site_name,
        "url": source_url,
        "base_url": _base_url(source_url),
        "catalog_key": custom_catalog_key(source_url),
        "engine_options": {},
    }
    providers = default_providers()
    detection = None
    catalog = None
    validation_errors: list[str] = []
    for candidate in candidates:
        if candidate.confidence <= 0:
            continue
        provider = providers[candidate.engine_id]
        config = {**base_config, "engine_id": candidate.engine_id}
        try:
            discovered = provider.discover(config, target_date or date.today().isoformat())
            validation = CatalogService.validate_catalog(discovered)
            provider_validation = provider.validate(discovered)
            errors = validation.errors + provider_validation.errors
            if errors:
                validation_errors.append(f"{candidate.engine_id}: {' '.join(errors)}")
                continue
            detection = candidate
            catalog = discovered
            break
        except Exception as exc:
            validation_errors.append(f"{candidate.engine_id}: {exc}")
    if detection is None or catalog is None:
        detail = validation_errors[0] if validation_errors else "일치하는 엔진 지문이 없습니다."
        raise ValueError(f"호환 가능한 예약 엔진을 확인하지 못했습니다. {detail}")

    catalog.last_checked_at = utc_now_iso()
    catalog.last_success_at = catalog.last_checked_at
    projected = catalog_to_site_config(catalog)
    projected.update(
        {
            "name": site_name,
            "url": source_url,
            "base_url": catalog.metadata.get("base_url", _base_url(source_url)),
            "style": legacy_style_for_engine(detection.engine_id),
            "engine_id": detection.engine_id,
            "catalog_key": base_config["catalog_key"],
            "engine_options": catalog.metadata.get("engine_options", {}),
            "detection": {
                "confidence": detection.confidence,
                "evidence": detection.evidence,
                "candidates": [
                    {"engine_id": item.engine_id, "confidence": item.confidence}
                    for item in candidates[:3]
                ],
            },
            "catalog": catalog,
        }
    )
    return projected


def legacy_style_for_engine(engine_id: str) -> str:
    return {
        "naver": "naver",
        "jigubyeol": "jigubyeol",
        "keyescape": "zeroworld",
        "sinbiworld": "zeroworld",
        "doomescape": "zeroworld",
        "zeroworld_laravel": "zeroworld",
    }.get(engine_id, "zeroworld")


def engine_id_for_legacy_style(style: str) -> str:
    return {"naver": "naver", "jigubyeol": "jigubyeol"}.get(style, "zeroworld_laravel")


def catalog_to_site_config(catalog: SiteCatalog, *, rich_keyescape: bool = False) -> dict[str, Any]:
    branches: dict[str, str] = {}
    branch_ids: dict[str, str] = {}
    themes: dict[str, dict[str, Any]] = {}
    theme_ids: dict[str, dict[str, str]] = {}
    branch_metadata: dict[str, dict[str, Any]] = {}
    theme_metadata: dict[str, dict[str, dict[str, Any]]] = {}
    for branch in catalog.branches.values():
        branches[branch.name] = branch.booking_value
        branch_ids[branch.name] = branch.id
        branch_metadata[branch.booking_value] = copy.deepcopy(branch.metadata)
        branch_themes: dict[str, Any] = {}
        branch_theme_ids: dict[str, str] = {}
        branch_theme_metadata: dict[str, dict[str, Any]] = {}
        for theme in branch.themes.values():
            if rich_keyescape and catalog.engine_id == "keyescape":
                branch_themes[theme.name] = {
                    "info_num": theme.booking_value,
                    "theme_num": str(theme.metadata.get("theme_num", "")),
                }
            else:
                branch_themes[theme.name] = theme.booking_value
            branch_theme_ids[theme.name] = theme.id
            branch_theme_metadata[theme.booking_value] = copy.deepcopy(theme.metadata)
        themes[branch.booking_value] = branch_themes
        theme_ids[branch.booking_value] = branch_theme_ids
        theme_metadata[branch.booking_value] = branch_theme_metadata
    return {
        "branches": branches,
        "branch_ids": branch_ids,
        "themes": themes,
        "theme_ids": theme_ids,
        "branch_metadata": branch_metadata,
        "theme_metadata": theme_metadata,
        "has_weekday_weekend": False,
    }


def migrate_custom_sites(custom_sites: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    migrated = copy.deepcopy(custom_sites) if isinstance(custom_sites, dict) else {}
    changed = False
    for name, config in migrated.items():
        if not isinstance(config, dict):
            continue
        if config.pop("pending_engine_detection", None) is not None:
            changed = True
        if not config.get("engine_id"):
            config["engine_id"] = engine_id_for_legacy_style(str(config.get("style", "")))
            changed = True
        if not config.get("catalog_key"):
            config["catalog_key"] = custom_catalog_key(str(config.get("url", name)))
            changed = True
        if "engine_options" not in config:
            config["engine_options"] = {"base_url": config.get("base_url", _base_url(str(config.get("url", ""))))}
            changed = True
    return migrated, changed


def builtin_site_configs() -> dict[str, dict[str, Any]]:
    from data.themes import SITES_CONFIG

    definitions = {
        "제로월드": ("builtin:zeroworld", "sinbiworld"),
        "지구별방탈출": ("builtin:jigubyeol", "jigubyeol"),
        "키이스케이프": ("builtin:keyescape", "keyescape"),
        "둠이스케이프": ("builtin:doomescape", "doomescape"),
    }
    result: dict[str, dict[str, Any]] = {}
    for name, (catalog_key, engine_id) in definitions.items():
        source = copy.deepcopy(SITES_CONFIG[name])
        source.update(
            {
                "name": name,
                "catalog_key": catalog_key,
                "engine_id": engine_id,
                "base_url": _base_url(source["url"]),
                "engine_options": {},
            }
        )
        result[name] = source
    return result


def fallback_catalog(site_name: str, site_config: dict[str, Any]) -> SiteCatalog:
    from data.themes import (
        DOOMESCAPE_THEMES,
        JIGUBYEOL_THEMES,
        KEYESCAPE_THEMES,
        ZEROWORLD_THEMES,
    )
    from engines.zeroworld_catalog import subject_for_branch

    engine_id = str(site_config["engine_id"])
    theme_sources = {
        "제로월드": ZEROWORLD_THEMES,
        "지구별방탈출": JIGUBYEOL_THEMES,
        "키이스케이프": KEYESCAPE_THEMES,
        "둠이스케이프": DOOMESCAPE_THEMES,
    }
    source_themes = theme_sources.get(site_name, site_config.get("themes", {}))
    branches: dict[str, CatalogBranch] = {}
    for branch_name, booking_value_raw in site_config.get("branches", {}).items():
        booking_value = str(booking_value_raw)
        subject = subject_for_branch(booking_value) if engine_id == "sinbiworld" else ""
        stable_id = f"{subject}:{booking_value}" if subject else booking_value
        themes: dict[str, CatalogTheme] = {}
        for theme_name, raw_value in source_themes.get(booking_value, {}).items():
            if isinstance(raw_value, dict):
                theme_id = str(raw_value.get("info_num", raw_value.get("id", "")))
                metadata = copy.deepcopy(raw_value)
            else:
                theme_id = str(raw_value)
                metadata = {}
            if theme_id:
                themes[theme_id] = CatalogTheme(theme_id, str(theme_name), theme_id, metadata)
        branch_metadata = {"subject": subject} if subject else {}
        branches[stable_id] = CatalogBranch(
            stable_id,
            str(branch_name),
            booking_value,
            metadata=branch_metadata,
            themes=themes,
        )
    base = str(site_config.get("base_url") or _base_url(site_config["url"]))
    return _site_catalog(
        site_config,
        engine_id,
        branches,
        {"base_url": base, "engine_options": copy.deepcopy(site_config.get("engine_options", {}))},
    )
