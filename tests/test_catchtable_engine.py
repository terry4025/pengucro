import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from engines.catchtable_engine import CatchTableEngine
from engines.catchtable_models import (
    CatchTableHoldingResult,
    CatchTableRsaKey,
    CatchTableSessionValidation,
    CatchTableTimeSlot,
)
from engines.registry import EngineRegistry


def test_registry_creates_catchtable_engine():
    logs = []
    successes = []
    engine = EngineRegistry.create(
        site_name="캐치테이블",
        mode="일반 사이트",
        payload={"site_url": "https://app.catchtable.co.kr"},
        custom_sites={},
        log_callback=lambda msg, color: logs.append((msg, color)),
        success_callback=lambda: successes.append(True),
    )
    assert isinstance(engine, CatchTableEngine)
    assert engine.site_url == "https://app.catchtable.co.kr"


def test_registry_creates_catchtable_from_custom_sites():
    engine = EngineRegistry.create(
        site_name="커스텀다이닝",
        mode="일반 사이트",
        payload={},
        custom_sites={"커스텀다이닝": {"engine_id": "catchtable", "url": "https://app.catchtable.co.kr"}},
        log_callback=lambda m, c: None,
        success_callback=lambda: None,
    )
    assert isinstance(engine, CatchTableEngine)


def test_engine_build_config_login_and_guest_modes():
    engine = CatchTableEngine(log_callback=lambda m, c: None)
    
    # Case 1: Default / use_login=True
    config1 = engine._build_config({
        "shop_alias": "ryunique",
        "date": "2026-08-25",
        "time_priorities": ["19:00", "18:30"],
        "persons": 4,
        "name": "홍길동",
        "phone": "010-1234-5678",
        "auth_token": "token_abc",
        "auto_create": True,
        "use_login": True,
    })
    assert config1.use_login is True
    assert config1.auth_token == "token_abc"
    assert config1.person_count == 4

    # Case 2: Guest mode / use_login=False
    config2 = engine._build_config({
        "shop_alias": "ryunique",
        "date": "2026-08-25",
        "use_login": False,
        "auth_token": "token_abc",
    })
    assert config2.use_login is False
    assert config2.auth_token == ""
    assert config2.cookies == {}


def test_engine_async_run_flow_with_login():
    async def _run():
        logs = []
        successes = []

        engine = CatchTableEngine(
            log_callback=lambda msg, color: logs.append((msg, color)),
            success_callback=lambda: successes.append(True),
        )

        mock_client = MagicMock()
        mock_client.validate_session = AsyncMock(return_value=CatchTableSessionValidation(
            is_valid=True,
            user_name="홍길동",
            user_phone="010-1111-2222",
            user_email="hong@test.com",
            user_seq=1234,
        ))
        mock_client.resolve_shop = AsyncMock(return_value={"shopRef": "ref_123", "shopName": "테스트식당"})
        mock_client.fetch_rsa_key = AsyncMock(return_value=CatchTableRsaKey(public_key_pem="fake_pem"))
        mock_client.get_time_slots = AsyncMock(return_value=[
            CatchTableTimeSlot(
                time="1830",
                date="260825",
                shop_ref="ref_123",
                table_type="H",
                available_yn=True,
                menu_set_seq=1,
            ),
            CatchTableTimeSlot(
                time="1900",
                date="260825",
                shop_ref="ref_123",
                table_type="H",
                available_yn=True,
                menu_set_seq=2,
            ),
        ])
        mock_client.request_holding = AsyncMock(return_value=CatchTableHoldingResult(
            holding_seq=112233,
            shop_ref="ref_123",
            visit_date="260825",
            visit_time="1900",
            person_count=2,
            table_type="H",
            deposit_required=False,
        ))
        mock_client.create_reservation = AsyncMock(return_value={"resultCode": "SUCCESS"})
        mock_client.close = AsyncMock()

        with patch("engines.catchtable_engine.CatchTableClient", return_value=mock_client):
            config = engine._build_config({
                "shop_alias": "test_shop",
                "date": "2026-08-25",
                "time_priorities": ["19:00", "18:30"],
                "persons": 2,
                "auto_create": True,
                "use_login": True,
            })
            await engine._async_run(config)

        # Verify session validation called
        mock_client.validate_session.assert_awaited_once()

        # Verify priority matching picked 19:00 over 18:30
        mock_client.request_holding.assert_awaited_once_with(
            shop_ref="ref_123",
            visit_yymmdd="260825",
            visit_hhmi="1900",
            person_count=2,
            table_type="H",
            menu_set_seqs=[2],
        )
        # Verify user credentials backfilled from session validation
        mock_client.create_reservation.assert_awaited_once_with(
            holding_seq=112233,
            user_name="홍길동",
            user_phone="010-1111-2222",
            user_email="hong@test.com",
        )
        assert len(successes) == 1
        assert any("로그인 확인" in log[0] for log in logs)
        assert any("선점 성공" in log[0] for log in logs)

    asyncio.run(_run())


def test_engine_async_run_flow_guest_mode():
    async def _run():
        logs = []
        successes = []

        engine = CatchTableEngine(
            log_callback=lambda msg, color: logs.append((msg, color)),
            success_callback=lambda: successes.append(True),
        )

        mock_client = MagicMock()
        mock_client.validate_session = AsyncMock()
        mock_client.resolve_shop = AsyncMock(return_value={"shopRef": "ref_123", "shopName": "테스트식당"})
        mock_client.fetch_rsa_key = AsyncMock(return_value=CatchTableRsaKey(public_key_pem="fake_pem"))
        mock_client.get_time_slots = AsyncMock(return_value=[
            CatchTableTimeSlot(
                time="1900",
                date="260825",
                shop_ref="ref_123",
                table_type="H",
                available_yn=True,
            ),
        ])
        mock_client.request_holding = AsyncMock(return_value=CatchTableHoldingResult(
            holding_seq=999999,
            shop_ref="ref_123",
            visit_date="260825",
            visit_time="1900",
            person_count=2,
            table_type="H",
            deposit_required=False,
        ))
        mock_client.close = AsyncMock()

        with patch("engines.catchtable_engine.CatchTableClient", return_value=mock_client):
            config = engine._build_config({
                "shop_alias": "test_shop",
                "date": "2026-08-25",
                "use_login": False,
                "auto_create": False,
            })
            await engine._async_run(config)

        # In guest mode, validate_session should NOT be called
        mock_client.validate_session.assert_not_awaited()
        mock_client.request_holding.assert_awaited_once()
        assert any("비로그인(익명)" in log[0] for log in logs)
        assert any("선점 성공" in log[0] for log in logs)

    asyncio.run(_run())
