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
from engines.cgv_browser_client import CgvBrowserClient
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
    "www.dpsnnn.com": "dpsnnn",
    "dpsnnn.com": "dpsnnn",
    "dpsnnn-s.imweb.me": "dpsnnn",
    "cgv.co.kr": "cgv",
    "www.cgv.co.kr": "cgv",
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


class CgvProvider(BaseProvider):
    engine_id = "cgv"

    def detect(self, url: str, html: str) -> DetectionResult:
        host = urllib.parse.urlparse(url).netloc.casefold()
        score = 100 if KNOWN_ENGINES.get(host) == self.engine_id else 0
        evidence = ["CGV 공식 예매 도메인"] if score else []
        if "/cnm/movieBook/" in url:
            score = min(100, score + 20)
            evidence.append("CGV 영화 예매 경로")
        return DetectionResult(self.engine_id, score, evidence)

    def discover(self, site_config: dict[str, Any], target_date: str) -> SiteCatalog:
        del target_date
        options = dict(site_config.get("engine_options", {}))
        snapshot = CgvBrowserClient().fetch_catalog()
        branches = {
            site.site_no: CatalogBranch(
                site.site_no,
                site.label,
                site.site_no,
                metadata={"region_code": site.region_code},
            )
            for site in snapshot.sites
        }
        return _site_catalog(
            site_config,
            self.engine_id,
            branches,
            {
                "base_url": "https://cgv.co.kr",
                "engine_options": options,
            },
        )


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


class DpsnnnProvider(BaseProvider):
    engine_id = "dpsnnn"

    def detect(self, url: str, html: str) -> DetectionResult:
        host = urllib.parse.urlparse(url).netloc.casefold()
        evidence: list[str] = []
        official = KNOWN_ENGINES.get(host) == self.engine_id
        score = 100 if official else 0
        if official:
            evidence.append("단편선 공식 도메인")
        for token, points, label in (
            ("SITE_BOOKING.init_calendar", 45, "아임웹 예약 달력"),
            ("/js/site_booking.js", 30, "아임웹 예약 모듈"),
            ('data-widget-type="booking"', 25, "예약 위젯"),
        ):
            if token in html:
                score = min(100 if official else 75, score + points)
                evidence.append(label)
        return DetectionResult(self.engine_id, score, evidence)

    def discover(self, site_config: dict[str, Any], target_date: str) -> SiteCatalog:
        from engines.dpsnnn_engine import DPSNNN_BRANCHES

        host = urllib.parse.urlparse(str(site_config.get("url", ""))).netloc.casefold()
        if KNOWN_ENGINES.get(host) != self.engine_id:
            raise ValueError("단편선 공식 예약 도메인이 아닙니다.")

        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        branches: dict[str, CatalogBranch] = {}
        for branch_id, raw in DPSNNN_BRANCHES.items():
            branch_config = copy.deepcopy(raw)
            reserve_url = urllib.parse.urljoin(
                branch_config["base_url"] + "/",
                branch_config["reserve_path"].lstrip("/"),
            )
            response = session.get(reserve_url, timeout=10)
            response.raise_for_status()
            if "SITE_BOOKING" not in response.text and "booking" not in response.text.casefold():
                raise ValueError(f"{branch_config['name']} 예약 페이지를 확인하지 못했습니다.")

            calendar = session.post(
                urllib.parse.urljoin(branch_config["base_url"] + "/", "booking/html_list.cm"),
                data={
                    "target_month": target_date[:7],
                    "select_day": target_date,
                    "menu_code": branch_config["menu_code"],
                },
                headers={"Referer": reserve_url, "Origin": branch_config["base_url"]},
                timeout=10,
            )
            calendar.raise_for_status()
            themes: dict[str, CatalogTheme] = {}
            for alias, full_name in branch_config["themes"].items():
                themes[alias] = CatalogTheme(
                    alias,
                    full_name,
                    alias,
                    {"alias": alias, "full_name": full_name},
                )
            branches[branch_id] = CatalogBranch(
                branch_id,
                branch_config["name"],
                branch_id,
                metadata=branch_config,
                themes=themes,
            )

        return _site_catalog(
            site_config,
            self.engine_id,
            branches,
            {
                "base_url": "https://www.dpsnnn.com",
                "engine_options": {"branches": copy.deepcopy(DPSNNN_BRANCHES)},
            },
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


class TripComProvider(BaseProvider):
    """Build a refreshable catalog from Trip.com's public campaign metadata."""

    engine_id = "tripcom"

    def detect(self, url: str, html: str) -> DetectionResult:
        host = urllib.parse.urlparse(url).netloc.casefold()
        matched = KNOWN_ENGINES.get(host) == self.engine_id
        evidence = ["Trip.com 공식 도메인"] if matched else []
        if "__foxpage_data__" in html and "campaignId" in html:
            evidence.append("Trip.com 캠페인 데이터")
        score = 100 if matched else (85 if evidence else 0)
        return DetectionResult(self.engine_id, score, evidence)

    def discover(self, site_config: dict[str, Any], target_date: str) -> SiteCatalog:
        import importlib

        tripcom = importlib.import_module("engines.tripcom_client")
        TripComClient = tripcom.TripComClient
        TripComError = tripcom.TripComError
        options = dict(site_config.get("engine_options", {}))
        max_campaigns = max(1, min(int(options.get("max_campaigns", 3)), 30))
        client = TripComClient(timeout=float(options.get("request_timeout", 12.0)))
        events = client.discover_events(max_campaigns=max_campaigns)
        branches: dict[str, CatalogBranch] = {}
        for event in events:
            action_kind = str(event.metadata().get("action_kind", "coupon"))
            branch_suffix = (
                f":{event.metadata().get('schema_id', '')}"
                if action_kind in {"hotel_flash_sale", "flight_flash_sale"}
                else ":flight-coupon" if action_kind == "flight_coupon" else ""
            )
            branch_id = f"campaign:{event.campaign_id}{branch_suffix}"
            branch = branches.get(branch_id)
            if branch is None:
                branch_name = event.campaign_name
                if action_kind == "hotel_flash_sale":
                    if event.metadata().get("section_id") == "hotelonepricedeal":
                        branch_name = "국내 럭셔리 호텔 5만원 찬스"
                    else:
                        branch_name = (
                            f"{event.campaign_name} · 호텔 핫딜 "
                            f"{event.metadata().get('schema_id', '')}"
                        )
                elif action_kind == "flight_flash_sale":
                    branch_name = (
                        "항공 1만원 초특가"
                        if event.metadata().get("section_id") == "flightonepricedeal"
                        else f"{event.campaign_name} · 항공 초특가"
                    )
                elif action_kind == "flight_coupon":
                    branch_name = f"{event.campaign_name} · 항공 선착순 할인코드"
                branch = CatalogBranch(
                    branch_id,
                    branch_name,
                    branch_id,
                    metadata={
                        "campaign_id": event.campaign_id,
                        "campaign_url": event.campaign_url,
                    },
                    themes={},
                )
                branches[branch_id] = branch
            label_parts = [event.event_name]
            status = str(event.metadata().get("sale_status", ""))
            if status == "preheat":
                label_parts.append("오픈 전")
            elif status == "flash_sale":
                label_parts.append(
                    "초특가 판매 중"
                    if action_kind == "flight_flash_sale"
                    else "5만원 판매 중"
                )
            elif status == "backup_sale":
                label_parts.append("5만원 종료 · 일반 특가")
            elif status == "sold_out":
                label_parts.append("매진")
            elif status == "ended":
                label_parts.append("종료")
            elif event.app_only:
                label_parts.append("앱 전용")
            elif action_kind == "flight_coupon" and event.in_stock is True:
                label_parts.append("발급 가능")
            elif event.in_stock is False:
                label_parts.append("현재 소진")
            label = " · ".join(label_parts)
            existing_names = {theme.name for theme in branch.themes.values()}
            if label in existing_names:
                event_date = event.allowed_dates[0] if event.allowed_dates else "날짜 미정"
                label = f"{label} · {event_date} {event.open_time}"
            if label in existing_names:
                label = f"{label} · {event.play_id}"
            branch.themes[event.event_id] = CatalogTheme(
                event.event_id,
                label,
                event.event_id,
                event.metadata(),
            )
        if not branches:
            raise TripComError("진행 중인 Trip.com 핫딜 이벤트를 찾지 못했습니다.")
        return _site_catalog(
            site_config,
            self.engine_id,
            branches,
            {
                "base_url": "https://kr.trip.com",
                "server_clock_url": "https://kr.trip.com/",
                "catalog_model": "tripcom-flash-v2",
                "authoritative_dynamic_catalog": True,
                "refresh_interval_seconds": int(options.get("refresh_interval_seconds", 600)),
                "engine_options": {
                    "max_campaigns": max_campaigns,
                    "request_timeout": float(options.get("request_timeout", 12.0)),
                },
            },
        )

    def validate(self, candidate: SiteCatalog) -> ValidationResult:
        base = super().validate(candidate)
        errors = list(base.errors)
        if not candidate.branches:
            errors.append("Trip.com 이벤트가 없습니다.")
        for branch in candidate.branches.values():
            for event in branch.themes.values():
                metadata = event.metadata
                if metadata.get("action_kind") == "hotel_flash_sale":
                    required = (
                        "campaign_id",
                        "schema_id",
                        "product_pk_id",
                        "hotel_id",
                        "room_id",
                        "campaign_url",
                        "open_at",
                    )
                elif metadata.get("action_kind") == "flight_flash_sale":
                    required = (
                        "campaign_id",
                        "schema_id",
                        "product_pk_id",
                        "product_id",
                        "stock_id",
                        "activity_code",
                        "campaign_url",
                        "product_url",
                        "open_at",
                    )
                else:
                    required = (
                        "campaign_id",
                        "play_id",
                        "prize_id",
                        "campaign_url",
                        "open_at",
                    )
                missing = [key for key in required if not metadata.get(key)]
                if missing:
                    errors.append(f"{event.name}: 필수 정보 누락({', '.join(missing)})")
        return ValidationResult(not errors, errors)


def default_providers() -> dict[str, BaseProvider]:
    providers: list[BaseProvider] = [
        CgvProvider(),
        SinbiWorldProvider(),
        DoomescapeProvider(),
        JigubyeolProvider(),
        KeyescapeProvider(),
        DpsnnnProvider(),
        NaverProvider(),
    ]
    return {provider.engine_id: provider for provider in providers}

def crawl_all_subpages(start_url: str, max_pages: int = 15) -> str:
    """
    start_url의 도메인을 기준으로 내부의 모든 서브페이지를 탐색하여 HTML 소스를 수집합니다.
    """
    import urllib.parse
    from bs4 import BeautifulSoup
    import logging

    parsed_start = urllib.parse.urlparse(start_url)
    start_domain = parsed_start.netloc.lower()
    if not start_domain:
        return ""

    visited = set()
    to_visit = [start_url]
    html_contents = []

    # 제외할 확장자 목록
    exclude_exts = {".png", ".jpg", ".jpeg", ".gif", ".css", ".js", ".pdf", ".zip", ".ico", ".svg", ".woff", ".woff2", ".ttf"}

    logging.info(f"[스파이더링 시작] {start_url} (최대 {max_pages} 페이지 탐색)")

    while to_visit and len(visited) < max_pages:
        current_url = to_visit.pop(0)
        # 쿼리 파라미터 제외하고 정규화된 URL 기준 중복 처리
        normalized_url = current_url.split("#")[0].rstrip("/")
        if normalized_url in visited:
            continue

        visited.add(normalized_url)

        # 확장자 체크
        parsed_current = urllib.parse.urlparse(current_url)
        path = parsed_current.path.lower()
        if any(path.endswith(ext) for ext in exclude_exts):
            continue

        # 동일 도메인 체크 (subdomain 포함 여부 등 유연하게 체크)
        current_domain = parsed_current.netloc.lower()
        if start_domain not in current_domain and current_domain not in start_domain:
            continue

        try:
            # 3초 타임아웃으로 지연 최소화
            response = requests.get(current_url, headers={"User-Agent": USER_AGENT}, timeout=3)
            if response.status_code != 200:
                continue

            html = response.text
            html_contents.append(f"<!-- Source: {current_url} -->\n" + html)

            # 서브페이지 파싱 및 수집
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith("javascript:") or href.startswith("mailto:") or href.startswith("tel:"):
                    continue

                # 절대 경로로 변환
                full_url = urllib.parse.urljoin(current_url, href)
                parsed_full = urllib.parse.urlparse(full_url)

                # 동일 도메인이고 아직 방문하지 않은 경우에만 큐에 추가
                full_domain = parsed_full.netloc.lower()
                if (start_domain in full_domain or full_domain in start_domain) and full_url.split("#")[0].rstrip("/") not in visited:
                    to_visit.append(full_url)

        except Exception as e:
            logging.debug(f"[스파이더링 오류] {current_url}: {e}")

    logging.info(f"[스파이더링 완료] 총 {len(html_contents)}개 페이지 수집 완료")
    return "\n".join(html_contents)


def rank_engine_candidates(url: str, html: str = "") -> list[DetectionResult]:
    providers = default_providers()
    if not html and not normalize_naver_url(url):
        html = crawl_all_subpages(url)
    elif html and not normalize_naver_url(url):
        html = html + "\n" + crawl_all_subpages(url)
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
        "dpsnnn": "zeroworld",
    }.get(engine_id, "zeroworld")


def engine_id_for_legacy_style(style: str) -> str:
    # Preserve the retired identifier when migrating saved settings. The
    # registry rejects it explicitly; never guess a replacement booking engine.
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
        "CGV": ("builtin:cgv", "cgv"),
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
                "engine_options": copy.deepcopy(source.get("engine_options", {})),
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
        "CGV": {},
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
