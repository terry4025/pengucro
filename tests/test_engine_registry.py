from engines.naver_engine import NaverEngine
from engines.doomescape_engine import DoomEscapeEngine
from engines.keyescape_engine import KeyescapeEngine
from engines.registry import EngineRegistry
from engines.zeroworld_shin_engine import ZeroWorldShinEngine
from pengucro.models import NAVER_MODE, STANDARD_MODE


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

    assert isinstance(key_engine, KeyescapeEngine)
    assert key_engine.api_url == "https://example.com/controller/run_proc.php"
    assert isinstance(doom_engine, DoomEscapeEngine)
    assert doom_engine.base_url == "https://example.com"
