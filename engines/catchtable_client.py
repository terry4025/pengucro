from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Mapping

import aiohttp

from engines.catchtable_crypto import CatchTableCrypto
from engines.catchtable_models import (
    CatchTableDaySlot,
    CatchTableHoldingResult,
    CatchTableRsaKey,
    CatchTableSessionValidation,
    CatchTableTimeSlot,
)

logger = logging.getLogger(__name__)


class CatchTableClient:
    """Async HTTP client for CatchTable core APIs."""

    DEFAULT_API_BASE = "https://ct-api.catchtable.co.kr"
    DEFAULT_APP_BASE = "https://app.catchtable.co.kr"
    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        *,
        api_base: str = DEFAULT_API_BASE,
        app_base: str = DEFAULT_APP_BASE,
        auth_token: str = "",
        device_id: str = "",
        cookies: Mapping[str, str] | None = None,
        timeout_seconds: float = 6.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.app_base = app_base.rstrip("/")
        self.auth_token = auth_token
        self.device_id = device_id or str(uuid.uuid4())
        self.cookies = dict(cookies or {})
        self.timeout_seconds = timeout_seconds
        self._session = session
        self._owns_session = False
        self._cached_rsa_key: CatchTableRsaKey | None = None
        self._transaction_counter = 0

    def _next_transaction_id(self) -> str:
        self._transaction_counter += 1
        return str(self._transaction_counter)

    def _get_headers(self, shop_ref: str = "") -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": self.DEFAULT_UA,
            "Accept": "application/json, text/plain, */*",
            "x-device-id": self.device_id,
            "x-transaction-id": self._next_transaction_id(),
            "x-requested-with": "XMLHttpRequest",
            "Origin": self.app_base,
            "Referer": f"{self.app_base}/",
        }
        if shop_ref:
            headers["shopRef"] = shop_ref
            headers["Referer"] = f"{self.app_base}/ct/shop/{shop_ref}"
        if self.auth_token:
            headers["Authorization"] = (
                self.auth_token if self.auth_token.startswith("Bearer ") else f"Bearer {self.auth_token}"
            )
        return headers

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                cookies=self.cookies,
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def fetch_rsa_key(self, force_refresh: bool = False) -> CatchTableRsaKey:
        """Retrieve RSA public key and anti-tamper parameters for slot queries."""
        if self._cached_rsa_key and not force_refresh:
            return self._cached_rsa_key

        session = await self.get_session()
        url = f"{self.api_base}/api/reservation/v1/dining/issue-key"
        headers = self._get_headers()

        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                payload = await resp.json()
                rsa_data = payload.get("data", {}).get("rsa", {})
                pub_key = rsa_data.get("slotEncPublicKey", "")
                if pub_key:
                    self._cached_rsa_key = CatchTableRsaKey(
                        public_key_pem=pub_key,
                        c1=int(rsa_data.get("c1", 0)),
                        c2=int(rsa_data.get("c2", 0)),
                        c3=int(rsa_data.get("c3", 0)),
                        c4=int(rsa_data.get("c4", 0)),
                    )
                    return self._cached_rsa_key

        # Fallback to /api/v3/init
        init_url = f"{self.api_base}/api/v3/init"
        async with session.get(init_url, headers=headers) as resp:
            resp.raise_for_status()
            payload = await resp.json()
            rsa_data = payload.get("data", {}).get("rsa", {})
            self._cached_rsa_key = CatchTableRsaKey(
                public_key_pem=rsa_data.get("slotEncPublicKey", ""),
                c1=int(rsa_data.get("c1", 0)),
                c2=int(rsa_data.get("c2", 0)),
                c3=int(rsa_data.get("c3", 0)),
                c4=int(rsa_data.get("c4", 0)),
            )
            return self._cached_rsa_key

    async def resolve_shop(self, shop_alias_or_ref: str) -> dict[str, Any]:
        """Resolve shop alias or ref to full shop details."""
        session = await self.get_session()
        headers = self._get_headers()

        # Try display check first
        check_url = f"{self.api_base}/api/display/v1/shops/{shop_alias_or_ref}/check"
        async with session.get(check_url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data

        # Try shop detail v4
        detail_url = f"{self.api_base}/api/v4/shops/{shop_alias_or_ref}"
        async with session.get(detail_url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("data", {}).get("shopDetailVO", data.get("data", {}))

    async def get_day_slots(
        self,
        shop_ref: str,
        *,
        person_count: int = 2,
    ) -> list[CatchTableDaySlot]:
        """Fetch date availability calendar for the target shop."""
        session = await self.get_session()
        headers = self._get_headers(shop_ref=shop_ref)
        url = f"{self.api_base}/api/reservation/v1/dining/day-slots"
        params = {
            "shopRef": shop_ref,
            "tableSeqs": "",
            "personCounts": str(person_count) if person_count else "",
        }

        async with session.get(url, headers=headers, params=params) as resp:
            resp.raise_for_status()
            body = await resp.json()
            slots_raw = body.get("data", [])
            results: list[CatchTableDaySlot] = []
            for item in slots_raw:
                results.append(
                    CatchTableDaySlot(
                        date=item.get("date", ""),
                        available_status=item.get("availableStatus", "CLOSED"),
                        available_person_counts=tuple(item.get("availablePersonCounts", [])),
                        benefit=item.get("benefit"),
                    )
                )
            return results

    async def get_time_slots(
        self,
        shop_ref: str,
        search_date: str,
        *,
        person_count: int = 2,
        table_type: str = "_ALL_",
        rsa_key: CatchTableRsaKey | None = None,
    ) -> list[CatchTableTimeSlot]:
        """Query available time slots for a specific date with RSA encrypted payload."""
        if rsa_key is None:
            rsa_key = await self.fetch_rsa_key()

        encrypted_param = CatchTableCrypto.encrypt_slot_params(
            rsa_key.public_key_pem,
            shop_ref=shop_ref,
            search_date=search_date,
            visit_time="19:00",
            person_count=person_count,
            table_type=table_type,
        )

        session = await self.get_session()
        headers = self._get_headers(shop_ref=shop_ref)
        headers["Content-Type"] = "application/json;charset=UTF-8"
        headers["t"] = str(int(time.time() * 1000))

        post_body = {
            "shopRef": shop_ref,
            "encryptedParamString": encrypted_param,
            "tableSeqs": [],
            "c1": rsa_key.c1,
            "c2": rsa_key.c2,
            "c3": rsa_key.c3,
            "c4": rsa_key.c4,
        }

        url = f"{self.api_base}/api/reservation/v1/dining/time-slots?shopRef={shop_ref}"
        async with session.post(url, headers=headers, json=post_body) as resp:
            if resp.status == 406:
                # RSA key expired, refresh and retry once
                logger.info("CatchTable RSA key expired (406), refreshing key...")
                rsa_key = await self.fetch_rsa_key(force_refresh=True)
                return await self.get_time_slots(
                    shop_ref,
                    search_date,
                    person_count=person_count,
                    table_type=table_type,
                    rsa_key=rsa_key,
                )

            resp.raise_for_status()
            data = await resp.json()
            time_slot_map = data.get("data", {}).get("timeSlotMap", {})
            results: list[CatchTableTimeSlot] = []

            for time_key, slot_info in time_slot_map.items():
                if not isinstance(slot_info, dict):
                    continue
                results.append(
                    CatchTableTimeSlot(
                        time=slot_info.get("time", time_key),
                        date=slot_info.get("date", search_date),
                        shop_ref=slot_info.get("shopRef", shop_ref),
                        table_type=slot_info.get("tableType", "H"),
                        period_gubun=slot_info.get("periodGubun", "D"),
                        available_yn=bool(slot_info.get("availableYn", False)),
                        menu_set_seq=slot_info.get("menuSetSeq"),
                        menu_set_seq_comma_list=str(slot_info.get("menuSetSeqCommaList", "")),
                        online_notice_seq=slot_info.get("onlineNoticeSeq"),
                        resp2=str(slot_info.get("resp2", "")),
                        raw_data=slot_info,
                    )
                )
            return sorted(results, key=lambda s: s.time)

    async def request_holding(
        self,
        *,
        shop_ref: str,
        visit_yymmdd: str,
        visit_hhmi: str,
        person_count: int = 2,
        table_type: str = "H",
        menu_set_seqs: list[int] | None = None,
        prev_holding_seq: int | None = None,
        table_seqs: list[int] | None = None,
    ) -> CatchTableHoldingResult:
        """Acquire temporary holding lock (pre-emption) for 3-5 minutes."""
        # Normalize date to YYMMDD
        clean_date = visit_yymmdd.replace("-", "")
        if len(clean_date) == 8:
            clean_date = clean_date[2:]  # "20260825" -> "260825"

        clean_time = visit_hhmi.replace(":", "")

        session = await self.get_session()
        headers = self._get_headers(shop_ref=shop_ref)
        headers["Content-Type"] = "application/json;charset=UTF-8"

        payload = {
            "shopRef": shop_ref,
            "visitYymmdd": clean_date,
            "visitHhmi": clean_time,
            "personCount": int(person_count),
            "tableType": table_type,
            "menuSetSeqs": menu_set_seqs or [],
            "prevHoldingSeq": prev_holding_seq,
            "tableSeqs": table_seqs,
        }

        url = f"{self.api_base}/api/reservation/v2/dining/holdings"
        async with session.post(url, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
            body_data = data.get("data", {})
            holding_seq = body_data.get("holdingSeq", 0)

            return CatchTableHoldingResult(
                holding_seq=holding_seq,
                shop_ref=shop_ref,
                visit_date=clean_date,
                visit_time=clean_time,
                person_count=person_count,
                table_type=table_type,
                menu_set_seqs=tuple(menu_set_seqs or ()),
                deposit_required=bool(body_data.get("depositRequired", False)),
                deposit_amount=int(body_data.get("depositAmount", 0)),
                raw_data=body_data,
            )

    async def extend_holding(self, holding_seq: int) -> bool:
        """Extend active holding lock before timeout."""
        session = await self.get_session()
        headers = self._get_headers()
        url = f"{self.api_base}/api/reservation/v2/dining/holdings/{holding_seq}/extend"
        async with session.post(url, headers=headers) as resp:
            return resp.status == 200

    async def release_holding(self, holding_seq: int) -> bool:
        """Cancel/release holding lock."""
        session = await self.get_session()
        headers = self._get_headers()
        url = f"{self.api_base}/api/reservation/v2/dining/holdings/{holding_seq}"
        async with session.delete(url, headers=headers) as resp:
            return resp.status in (200, 204)

    async def create_reservation(
        self,
        *,
        holding_seq: int,
        user_name: str,
        user_phone: str,
        user_email: str = "",
        visit_purpose: str = "",
        payment_info: dict[str, Any] | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Finalize reservation after acquiring holding lock."""
        session = await self.get_session()
        headers = self._get_headers()
        headers["Content-Type"] = "application/json;charset=UTF-8"

        payload: dict[str, Any] = {
            "holdingSeq": holding_seq,
            "bookerName": user_name,
            "bookerPhone": user_phone,
            "bookerEmail": user_email,
            "visitorBookerIdentical": True,
            "visitorName": user_name,
            "visitorPhone": user_phone,
            "visitPurpose": visit_purpose or "식사",
            "version": "2",
        }
        if payment_info:
            payload["paymentInfo"] = payment_info
        if extra_fields:
            payload.update(extra_fields)

        url = f"{self.api_base}/api/reservation/v2/dinings/create"
        async with session.post(url, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data

    async def validate_session(self) -> CatchTableSessionValidation:
        """Validate whether current auth token/cookies are active and retrieve user profile."""
        if not self.auth_token and not any(k in self.cookies for k in ["ct_access_token", "accessToken"]):
            return CatchTableSessionValidation(
                is_valid=False,
                error_message="로그인 토큰 또는 세션 쿠키가 없습니다.",
            )

        session = await self.get_session()
        headers = self._get_headers()

        # Try user profile endpoint
        url = f"{self.api_base}/api/v3/user/profile"
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    user_data = data.get("data", {})
                    if user_data:
                        return CatchTableSessionValidation(
                            is_valid=True,
                            user_name=user_data.get("userName") or user_data.get("displayName") or "",
                            user_phone=user_data.get("phoneNumber") or "",
                            user_email=user_data.get("email") or "",
                            user_seq=int(user_data.get("userSeq") or 0),
                            raw_data=user_data,
                        )
                elif resp.status in (401, 403):
                    return CatchTableSessionValidation(
                        is_valid=False,
                        error_message="로그인 세션이 만료되었습니다. 다시 로그인해 주세요.",
                    )
        except Exception as e:
            logger.debug("Failed to validate session via /api/v3/user/profile: %s", e)

        # Fallback to user-info endpoint
        try:
            fallback_url = f"{self.api_base}/api/v3/user-info"
            async with session.get(fallback_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    user_data = data.get("data", {})
                    return CatchTableSessionValidation(
                        is_valid=True,
                        user_name=user_data.get("userName") or user_data.get("displayName") or "",
                        user_phone=user_data.get("phoneNumber") or "",
                        user_email=user_data.get("email") or "",
                        user_seq=int(user_data.get("userSeq") or 0),
                        raw_data=user_data,
                    )
        except Exception as e:
            logger.debug("Failed to validate session via /api/v3/user-info: %s", e)

        return CatchTableSessionValidation(
            is_valid=False,
            error_message="로그인 인증에 실패했습니다.",
        )
