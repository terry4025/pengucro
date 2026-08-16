import pytest

import engines.catalog_providers as provider_module
from engines.catalog_providers import (
    CgvProvider,
    DpsnnnProvider,
    DoomescapeProvider,
    JigubyeolProvider,
    KeyescapeProvider,
    NaverProvider,
    SinbiWorldProvider,
    ZeroWorldLaravelProvider,
    analyze_booking_site,
    catalog_to_site_config,
    detect_engine,
    engine_id_for_legacy_style,
    migrate_custom_sites,
)
from engines.cgv_browser_client import CgvCatalogSnapshot
from engines.cgv_client import CgvRegion, CgvSite
from pengucro.catalog import (
    CatalogBranch,
    CatalogTheme,
    DetectionResult,
    SiteCatalog,
    ValidationResult,
)


class FakeResponse:
    def __init__(self, text="", payload=None):
        self.text = text
        self.content = text.encode("utf-8")
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_engine_fingerprints_recognize_supported_templates():
    assert DpsnnnProvider().detect(
        "https://www.dpsnnn.com/main",
        'SITE_BOOKING.init_calendar /js/site_booking.js data-widget-type="booking"',
    ).confidence == 100
    assert SinbiWorldProvider().detect(
        "https://example.com/reservation",
        "rev.make.sel.php fun_theme_time_list s_subj zizum_num",
    ).confidence == 100
    assert JigubyeolProvider().detect(
        "https://example.com/reservation",
        '<meta name="csrf-token"> reservation/create branch=1 theme=2',
    ).confidence == 100
    assert KeyescapeProvider().detect(
        "https://example.com/reservation.php",
        'run_proc.php get_theme_info_list name="zizum" name="theme"',
    ).confidence == 100
    assert DoomescapeProvider().detect(
        "https://example.com/layout/res/home.php",
        'name="s_zizum" tm_box rev.make rev.act.php',
    ).confidence == 100


def test_cgv_provider_uses_browser_bff_catalog_without_manual_site(monkeypatch):
    class BrowserClient:
        def fetch_catalog(self):
            return CgvCatalogSnapshot(
                (CgvRegion("01", "서울", 1),),
                (CgvSite("0013", "용산아이파크몰", "01"),),
            )

    monkeypatch.setattr(provider_module, "CgvBrowserClient", BrowserClient)

    catalog = CgvProvider().discover(
        {"catalog_key": "builtin:cgv", "name": "CGV", "url": "https://cgv.co.kr"},
        "2026-08-18",
    )

    assert set(catalog.branches) == {"0013"}
    assert catalog.branches["0013"].name == "CGV 용산아이파크몰"
    assert catalog.branches["0013"].metadata["region_code"] == "01"


def test_keyescape_projection_keeps_both_theme_identifiers():
    catalog = SiteCatalog(
        "builtin:keyescape",
        "키이스케이프",
        "keyescape",
        "https://www.keyescape.com/reservation.php",
        {
            "26": CatalogBranch(
                "26",
                "에버랜드",
                "26",
                themes={
                    "71": CatalogTheme(
                        "71",
                        "Memory of Poppy",
                        "71",
                        {"info_num": "71", "theme_num": "78"},
                    )
                },
            )
        },
    )

    projected = catalog_to_site_config(catalog, rich_keyescape=True)

    assert projected["themes"]["26"]["Memory of Poppy"] == {
        "info_num": "71",
        "theme_num": "78",
    }
    assert projected["branch_ids"] == {"에버랜드": "26"}
    assert projected["theme_ids"]["26"] == {"Memory of Poppy": "71"}


def test_legacy_custom_site_gets_stable_engine_and_catalog_ids():
    sites, changed = migrate_custom_sites(
        {
            "테스트": {
                "url": "https://example.com/reservation",
                "style": "jigubyeol",
                "pending_engine_detection": {"engine_id": "zeroworld_laravel"},
            }
        }
    )

    assert changed
    assert sites["테스트"]["engine_id"] == "jigubyeol"
    assert sites["테스트"]["catalog_key"].startswith("custom:")
    assert "pending_engine_detection" not in sites["테스트"]
    assert engine_id_for_legacy_style("zeroworld") == "zeroworld_laravel"


def test_sinbiworld_fixture_discovers_subject_branches_and_themes(monkeypatch):
    class Session:
        def __init__(self):
            self.headers = {}

        def get(self, _url, params, timeout):
            subject = params["s_subj"]
            branch_id = "1" if subject == "A" else "2"
            name = "김포본점" if subject == "A" else "제로월드 다이브 건대점"
            return FakeResponse(
                f'<a href="?go=rev.make&amp;s_subj={subject}&amp;zizum_num={branch_id}">'
                f'<span class="rese-spot__text">[{name}] 제로월드</span></a>'
            )

        def post(self, _url, data, headers, timeout):
            theme_id = "101" if data["zizum_num"] == "1" else "202"
            return FakeResponse(
                f'<a href="javascript:fun_theme_select(\'{theme_id}\')">'
                f'<span class="choice-themes__name">테마 {theme_id}</span></a>'
            )

    monkeypatch.setattr(provider_module.requests, "Session", Session)
    config = {
        "catalog_key": "fixture:zero",
        "name": "제로 fixture",
        "url": "https://example.com/layout/res/home.php",
    }

    catalog = SinbiWorldProvider().discover(config, "2026-08-01")

    assert set(catalog.branches) == {"A:1", "B:2"}
    assert catalog.branches["B:2"].metadata["subject"] == "B"
    assert catalog.branches["A:1"].themes["101"].name == "테마 101"


def test_keyescape_fixture_keeps_info_theme_and_doing_metadata(monkeypatch):
    class Session:
        def __init__(self):
            self.headers = {}

        def get(self, _url, timeout):
            return FakeResponse(
                '<select name="zizum"><option value="14">강남 더오름</option></select>'
            )

        def post(self, _url, data, timeout):
            return FakeResponse(
                payload={
                    "status": True,
                    "data": [
                        {
                            "info_num": 71,
                            "level_num": 78,
                            "info_name": "Memory of Poppy",
                            "doing": 14,
                        }
                    ],
                }
            )

    monkeypatch.setattr(provider_module.requests, "Session", Session)
    config = {
        "catalog_key": "fixture:key",
        "name": "키 fixture",
        "url": "https://example.com/reservation.php",
    }

    catalog = KeyescapeProvider().discover(config, "2026-08-01")
    theme = catalog.branches["14"].themes["71"]

    assert theme.metadata == {"info_num": "71", "theme_num": "78", "doing": 14}


def test_doomescape_fixture_discovers_theme_id_from_image(monkeypatch):
    class Session:
        def __init__(self):
            self.headers = {}

        def get(self, _url, params, timeout):
            if "s_zizum" not in params:
                return FakeResponse(
                    '<select name="s_zizum"><option value="4">2호점</option></select>'
                )
            return FakeResponse(
                '<div class="tm_box"><div class="img_box">'
                '<img src="/img/theme/30_cover.jpg"></div>'
                '<div class="info_box"><div class="tit"><span class="name">운명</span>'
                '</div></div></div>'
            )

    monkeypatch.setattr(provider_module.requests, "Session", Session)
    config = {
        "catalog_key": "fixture:doom",
        "name": "둠 fixture",
        "url": "https://example.com/layout/res/home.php",
    }

    catalog = DoomescapeProvider().discover(config, "2026-08-01")

    assert catalog.branches["4"].themes["30"].name == "운명"


@pytest.mark.parametrize(
    ("provider", "engine_id"),
    [
        (JigubyeolProvider(), "jigubyeol"),
        (NaverProvider(), "naver"),
        (ZeroWorldLaravelProvider(), "zeroworld_laravel"),
    ],
)
def test_legacy_backed_provider_fixture_projects_catalog(monkeypatch, provider, engine_id):
    style = "jigubyeol" if engine_id == "jigubyeol" else "zeroworld"
    if engine_id == "naver":
        style = "naver"
    monkeypatch.setattr(
        provider_module,
        "parse_booking_site",
        lambda _url, _name: {
            "url": "https://example.com/reservation",
            "base_url": "https://example.com",
            "style": style,
            "branches": {"본점": "1"},
            "themes": {"1": {"Fixture Theme": "10"}},
        },
    )
    config = {
        "catalog_key": f"fixture:{engine_id}",
        "name": "fixture",
        "url": "https://example.com/reservation",
    }

    catalog = provider.discover(config, "2026-08-01")

    assert catalog.engine_id == engine_id
    assert catalog.branches["1"].themes["10"].name == "Fixture Theme"


def test_engine_detection_keeps_confidence_as_informational_ranking():
    html = (
        'rev.make.sel.php fun_theme_time_list s_subj zizum_num '
        'run_proc.php get_theme_info_list name="zizum" name="theme"'
    )

    best, candidates = detect_engine("https://example.com/reservation", html)

    assert best.confidence == 100
    assert candidates[1].confidence == 100


def test_new_site_auto_selects_catalog_verified_engine_even_with_low_score(monkeypatch):
    class CompatibleProvider:
        engine_id = "fixture_engine"

        def discover(self, config, target_date):
            return SiteCatalog(
                config["catalog_key"],
                config["name"],
                self.engine_id,
                config["url"],
                {
                    "1": CatalogBranch(
                        "1",
                        "본점",
                        "1",
                        themes={"10": CatalogTheme("10", "검증 테마", "10")},
                    )
                },
            )

        def validate(self, candidate):
            return ValidationResult(True, [])

    monkeypatch.setattr(
        provider_module,
        "rank_engine_candidates",
        lambda _url: [DetectionResult("fixture_engine", 60, ["참고 지문"])],
    )
    monkeypatch.setattr(
        provider_module,
        "default_providers",
        lambda: {"fixture_engine": CompatibleProvider()},
    )

    result = analyze_booking_site("https://example.com/reservation", "자동 등록 테스트")

    assert result["engine_id"] == "fixture_engine"
    assert result["detection"]["confidence"] == 60
    assert result["themes"]["1"] == {"검증 테마": "10"}
