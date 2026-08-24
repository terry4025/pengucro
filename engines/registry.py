from __future__ import annotations

from typing import Any

from engines.catchtable_engine import CatchTableEngine
from engines.cgv_engine_preopen_live_runtime import CgvEngine
from engines.doomescape_engine import DoomEscapeEngine
from engines.dpsnnn_engine import DpsnnnEngine
from engines.jigubyeol_engine import JigubyeolEngine
from engines.keyescape_engine_runtime import KeyescapeEngine
from engines.naver_engine import NaverEngine
from engines.zeroworld_gu_engine import ZeroWorldGuEngine
from engines.zeroworld_shin_engine import ZeroWorldShinEngine
from pengucro.models import NAVER_MODE, TRIPCOM_MODE


class EngineRegistry:
    """Creates the correct engine without coupling the GUI to engine classes."""

    @staticmethod
    def create(
        *,
        site_name: str,
        mode: str,
        payload: dict[str, Any],
        custom_sites: dict[str, dict[str, Any]],
        log_callback,
        success_callback,
        status_callback=None,
        log_batch_callback=None,
        event_callback=None,
    ):
        common = {
            "log_callback": log_callback,
            "success_callback": success_callback,
        }
        if mode == NAVER_MODE:
            engine = NaverEngine(**common)
        elif mode == TRIPCOM_MODE:
            import importlib

            engine = importlib.import_module("engines.tripcom_engine").TripComEngine(**common)
        elif site_name == "캐치테이블":
            engine = CatchTableEngine(
                site_url=payload.get("site_url", "https://app.catchtable.co.kr"),
                **common,
            )
        elif site_name == "CGV":
            engine = CgvEngine(**common)
        elif site_name == "제로월드":
            metadata = payload.get("engine_metadata", {})
            engine = ZeroWorldShinEngine(
                site_url=payload.get("site_url", ""),
                engine_options=metadata.get("engine_options", {}) if isinstance(metadata, dict) else {},
                **common,
            )
        elif site_name == "지구별방탈출":
            engine = JigubyeolEngine(**common)
        elif site_name == "키이스케이프":
            engine = KeyescapeEngine(**common)
        elif site_name == "둠이스케이프":
            engine = DoomEscapeEngine(site_url=payload.get("site_url", ""), **common)
        elif site_name in custom_sites:
            site = custom_sites[site_name]
            engine_id = site.get("engine_id")
            style = site.get("style")
            if engine_id == "jigubyeol" or (not engine_id and style == "jigubyeol"):
                engine = JigubyeolEngine(site_url=site.get("base_url"), **common)
            elif engine_id == "naver" or (not engine_id and style == "naver"):
                engine = NaverEngine(**common)
            elif engine_id == "keyescape":
                engine = KeyescapeEngine(site_url=site.get("base_url") or site.get("url", ""), **common)
            elif engine_id == "doomescape":
                engine = DoomEscapeEngine(site_url=site.get("base_url") or site.get("url", ""), **common)
            elif engine_id == "dpsnnn":
                metadata = payload.get("engine_metadata", {})
                payload_options = metadata.get("engine_options", {}) if isinstance(metadata, dict) else {}
                engine = DpsnnnEngine(
                    site_url=site.get("base_url") or site.get("url", ""),
                    engine_options=payload_options or site.get("engine_options", {}),
                    **common,
                )
            elif engine_id == "tripcom":
                import importlib

                engine = importlib.import_module("engines.tripcom_engine").TripComEngine(**common)
            elif engine_id == "cgv":
                engine = CgvEngine(**common)
            elif engine_id == "catchtable":
                engine = CatchTableEngine(site_url=site.get("url", ""), **common)
            elif engine_id == "sinbiworld":
                engine = ZeroWorldShinEngine(
                    site_url=site.get("url", ""),
                    engine_options=site.get("engine_options", {}),
                    **common,
                )
            else:
                engine = ZeroWorldGuEngine(site_url=site.get("url", ""), **common)
        else:
            raise ValueError(f"지원하지 않는 예약 사이트입니다: {site_name}")

        engine.status_callback = status_callback
        engine.log_batch_callback = log_batch_callback
        engine.event_callback = event_callback
        return engine
