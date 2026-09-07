import copy

import pytest

from engines.naver_engine import NaverEngine
from engines.cgv_engine import CgvEngine
from engines.doomescape_engine import DoomEscapeEngine
from engines.dpsnnn_engine import DpsnnnEngine
from engines.keyescape_engine import KeyescapeEngine
from engines.tripcom_engine import TripComEngine
from engines.registry import EngineRegistry
from engines.zeroworld_shin_engine import ZeroWorldShinEngine
from pengucro.models import NAVER_MODE, STANDARD_MODE, TRIPCOM_MODE


def noop(*args, **kwargs):
    return None


def test_registry_selects_current_zeroworld_engine():
    engine = EngineRegistry.create(
        site_name="제로월드",
        mode=STANDARD_MODE,
        payload={"site_url": "https://zeroworldkorea.com/layout/res/home.php?go=main"},
        custom_sites={},
        log_callback=noop,
        success_callback=noop,
    )
    assert isinstance(engine, ZeroWorldShinEngine)


@pytest.mark.parametrize("engine_id", ["sinbiworld", "zeroworld_shin"])
def test_registry_keeps_current_custom_zeroworld_engine(engine_id):
    engine = EngineRegistry.create(
        site_name="현재 제로월드",
        mode=STANDARD_MODE,
        payload={},
        custom_sites={"현재 제로월드": {"engine_id": engine_id, "url": "https://zero.example"}},
        log_callback=noop,
        success_callback=noop,
    )
    assert isinstance(engine, ZeroWorldShinEngine)


@pytest.mark.parametrize("legacy_config", [
    {"engine_id": "zeroworld_laravel"},
    {"engine_id": "zeroworld_gu"},
    {"style": "zeroworld"},
])
def test_registry_rejects_retired_zeroworld_without_changing_saved_site(legacy_config):
    sites = {"구형 사이트": {**legacy_config, "url": "https://old.example", "themes": {"1": {"테마": "2"}}}}
    original = copy.deepcopy(sites)
    with pytest.raises(ValueError, match="지원이 종료"):
        EngineRegistry.create(
            site_name="구형 사이트",
            mode=STANDARD_MODE,
            payload={},
            custom_sites=sites,
            log_callback=noop,
            success_callback=noop,
        )
    assert sites == original


@pytest.mark.parametrize("config", [{}, {"engine_id": "unknown"}, {"engine_id": "unknown", "style": "jigubyeol"}])
def test_registry_does_not_guess_an_engine_for_unknown_custom_site(config):
    with pytest.raises(ValueError, match="지원하지 않는 커스텀 예약 엔진"):
        EngineRegistry.create(
            site_name="미확인 사이트",
            mode=STANDARD_MODE,
            payload={},
            custom_sites={"미확인 사이트": {**config, "url": "https://unknown.example"}},
            log_callback=noop,
            success_callback=noop,
        )


def test_registry_selects_cgv_engine():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode=STANDARD_MODE,
        payload={},
        custom_sites={},
        log_callback=noop,
        success_callback=noop,
    )
    assert isinstance(engine, CgvEngine)


def test_registry_selects_naver_by_site_type():
    engine = EngineRegistry.create(
        site_name="테스트 네이버",
        mode=NAVER_MODE,
        payload={},
        custom_sites={"테스트 네이버": {"style": "naver"}},
        log_callback=noop,
        success_callback=noop,
    )
    assert isinstance(engine, NaverEngine)


def test_registry_selects_tripcom_by_dedicated_mode():
    engine = EngineRegistry.create(
        site_name="Trip.com 핫딜",
        mode=TRIPCOM_MODE,
        payload={},
        custom_sites={},
        log_callback=noop,
        success_callback=noop,
    )
    assert isinstance(engine, TripComEngine)


def test_registry_selects_detected_custom_engine_id():
    common = {
        "site_name": "호환 사이트",
        "mode": STANDARD_MODE,
        "payload": {},
        "log_callback": noop,
        "success_callback": noop,
    }
    key_engine = EngineRegistry.create(
        **common,
        custom_sites={"호환 사이트": {"engine_id": "keyescape", "base_url": "https://example.com"}},
    )
    doom_engine = EngineRegistry.create(
        **common,
        custom_sites={"호환 사이트": {"engine_id": "doomescape", "base_url": "https://example.com"}},
    )
    dpsnnn_engine = EngineRegistry.create(
        **common,
        custom_sites={
            "호환 사이트": {
                "engine_id": "dpsnnn",
                "base_url": "https://www.dpsnnn.com",
                "engine_options": {"branches": {}},
            }
        },
    )

    assert isinstance(key_engine, KeyescapeEngine)
    assert key_engine.api_url == "https://example.com/controller/run_proc.php"
    assert isinstance(doom_engine, DoomEscapeEngine)
    assert doom_engine.base_url == "https://example.com"
    assert isinstance(dpsnnn_engine, DpsnnnEngine)
    assert dpsnnn_engine.site_url == "https://www.dpsnnn.com"
