import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from engines.catchtable_client import CatchTableClient
from engines.catchtable_models import CatchTableRsaKey


@pytest.fixture
def mock_rsa_pem():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def test_client_fetch_rsa_key(mock_rsa_pem):
    async def _run():
        client = CatchTableClient(api_base="https://ct-api.mock.local")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "data": {
                "rsa": {
                    "slotEncPublicKey": mock_rsa_pem,
                    "c1": 5,
                    "c2": 2,
                    "c3": 1,
                    "c4": 0,
                }
            }
        })

        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_session.closed = False

        with patch.object(client, "get_session", AsyncMock(return_value=mock_session)):
            rsa_key = await client.fetch_rsa_key()
            assert rsa_key.public_key_pem == mock_rsa_pem
            assert rsa_key.c1 == 5
            assert rsa_key.c2 == 2

    asyncio.run(_run())


def test_client_get_day_slots():
    async def _run():
        client = CatchTableClient()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "data": [
                {
                    "date": "2026-08-25",
                    "availableStatus": "AVAILABLE",
                    "availablePersonCounts": [2, 3, 4],
                },
                {
                    "date": "2026-08-26",
                    "availableStatus": "CLOSED",
                    "availablePersonCounts": [],
                }
            ]
        })

        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch.object(client, "get_session", AsyncMock(return_value=mock_session)):
            slots = await client.get_day_slots("test_ref", person_count=2)
            assert len(slots) == 2
            assert slots[0].date == "2026-08-25"
            assert slots[0].is_available is True
            assert slots[1].is_available is False

    asyncio.run(_run())


def test_client_get_time_slots(mock_rsa_pem):
    async def _run():
        client = CatchTableClient()
        rsa_key = CatchTableRsaKey(public_key_pem=mock_rsa_pem, c1=1, c2=2, c3=3, c4=0)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "data": {
                "timeSlotMap": {
                    "1900": {
                        "time": "1900",
                        "date": "260825",
                        "shopRef": "test_ref",
                        "tableType": "H",
                        "availableYn": True,
                        "menuSetSeq": 101,
                    },
                    "2000": {
                        "time": "2000",
                        "date": "260825",
                        "shopRef": "test_ref",
                        "tableType": "R",
                        "availableYn": False,
                    }
                }
            }
        })

        mock_session = MagicMock()
        mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.post.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch.object(client, "get_session", AsyncMock(return_value=mock_session)):
            time_slots = await client.get_time_slots(
                "test_ref",
                search_date="2026-08-25",
                rsa_key=rsa_key,
            )
            assert len(time_slots) == 2
            assert time_slots[0].time == "1900"
            assert time_slots[0].formatted_time == "19:00"
            assert time_slots[0].available_yn is True
            assert time_slots[0].table_type == "H"

    asyncio.run(_run())


def test_client_request_holding():
    async def _run():
        client = CatchTableClient()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "data": {
                "holdingSeq": 987654,
                "depositRequired": False,
                "depositAmount": 0,
            }
        })

        mock_session = MagicMock()
        mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.post.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch.object(client, "get_session", AsyncMock(return_value=mock_session)):
            holding = await client.request_holding(
                shop_ref="test_ref",
                visit_yymmdd="2026-08-25",
                visit_hhmi="19:00",
                person_count=2,
                table_type="H",
            )
            assert holding.holding_seq == 987654
            assert holding.visit_date == "260825"
            assert holding.visit_time == "1900"
            assert holding.deposit_required is False

    asyncio.run(_run())


def test_client_validate_session_success():
    async def _run():
        client = CatchTableClient(auth_token="valid_token_123")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "data": {
                "userName": "홍길동",
                "phoneNumber": "010-9876-5432",
                "email": "hong@example.com",
                "userSeq": 5555,
            }
        })

        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch.object(client, "get_session", AsyncMock(return_value=mock_session)):
            validation = await client.validate_session()
            assert validation.is_valid is True
            assert validation.user_name == "홍길동"
            assert validation.user_phone == "010-9876-5432"
            assert validation.user_seq == 5555

    asyncio.run(_run())


def test_client_validate_session_missing_or_expired():
    async def _run():
        # Case 1: No token
        client = CatchTableClient(auth_token="")
        val1 = await client.validate_session()
        assert val1.is_valid is False
        assert "토큰" in val1.error_message

        # Case 2: Expired 401
        client2 = CatchTableClient(auth_token="expired_token")
        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch.object(client2, "get_session", AsyncMock(return_value=mock_session)):
            val2 = await client2.validate_session()
            assert val2.is_valid is False
            assert "만료" in val2.error_message

    asyncio.run(_run())

