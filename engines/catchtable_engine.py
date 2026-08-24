from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Mapping

from engines.async_hot_path import AsyncHotPathScheduler
from engines.base_engine import BaseEngine
from engines.catchtable_browser import load_saved_catchtable_session
from engines.catchtable_client import CatchTableClient
from engines.catchtable_models import (
    CatchTableBookingConfig,
    CatchTableHoldingResult,
    CatchTableRsaKey,
    CatchTableTimeSlot,
)
from engines.server_clock import ServerClock
from pengucro.diagnostics import format_exception
from pengucro.models import BookingEventType, parse_bool_flag

logger = logging.getLogger(__name__)


class CatchTableEngine(BaseEngine):
    """Engine for automated reservation and holding on CatchTable."""

    LOOKUP_TIMEOUT = 5
    SUBMIT_TIMEOUT = 8
    HOT_PATH_INTERVAL = 0.05  # 50ms polling loop on target time

    def __init__(
        self,
        log_callback,
        success_callback=None,
        status_callback=None,
        log_batch_callback=None,
        event_callback=None,
        site_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            log_callback=log_callback,
            success_callback=success_callback,
            status_callback=status_callback,
            log_batch_callback=log_batch_callback,
            event_callback=event_callback,
        )
        self.site_url = site_url or "https://app.catchtable.co.kr"
        self._holding_result: CatchTableHoldingResult | None = None
        self._active_client: CatchTableClient | None = None

    def _build_config(self, reservation_data: dict[str, Any]) -> CatchTableBookingConfig:
        """Parse raw payload from GUI into structured config."""
        saved_session = load_saved_catchtable_session()

        # Handle shop ref or alias from URL/metadata
        shop_raw = (
            reservation_data.get("shop_alias")
            or reservation_data.get("shop_ref")
            or reservation_data.get("theme")
            or reservation_data.get("site_url", "")
        )
        if "/shop/" in shop_raw or "/ct/shop/" in shop_raw:
            shop_raw = shop_raw.split("/shop/")[-1].split("?")[0].strip("/")

        target_date = str(reservation_data.get("date", "")).strip()

        # Parse time priorities
        priorities: list[str] = []
        raw_times = reservation_data.get("time_priorities") or reservation_data.get("time") or []
        if isinstance(raw_times, str):
            priorities = [t.strip() for t in raw_times.replace(",", ";").split(";") if t.strip()]
        elif isinstance(raw_times, (list, tuple)):
            priorities = [str(t).strip() for t in raw_times if str(t).strip()]

        person_count = int(reservation_data.get("persons") or reservation_data.get("person_count") or 2)
        table_type = str(reservation_data.get("table_type") or "_ALL_").strip()

        user_name = str(reservation_data.get("name") or reservation_data.get("user_name") or "").strip()
        user_phone = str(reservation_data.get("phone") or reservation_data.get("user_phone") or "").strip()
        user_email = str(reservation_data.get("email") or reservation_data.get("user_email") or "").strip()

        use_login = parse_bool_flag(reservation_data.get("use_login"), default=True)
        if not use_login:
            auth_token = ""
            device_id = str(reservation_data.get("device_id") or "").strip()
            cookies: dict[str, str] = {}
        else:
            auth_token = str(
                reservation_data.get("auth_token") or saved_session.get("auth_token") or ""
            ).strip()
            device_id = str(
                reservation_data.get("device_id") or saved_session.get("device_id") or ""
            ).strip()
            cookies = dict(saved_session.get("cookies", {}))
            if isinstance(reservation_data.get("cookies"), dict):
                cookies.update(reservation_data["cookies"])

        auto_create = bool(reservation_data.get("auto_create", False))
        open_time = str(reservation_data.get("open_time") or "").strip()

        return CatchTableBookingConfig(
            shop_alias_or_ref=shop_raw,
            target_date=target_date,
            person_count=person_count,
            time_priorities=tuple(priorities),
            table_type=table_type,
            user_name=user_name,
            user_phone=user_phone,
            user_email=user_email,
            use_login=use_login,
            auth_token=auth_token,
            device_id=device_id,
            cookies=cookies,
            auto_create=auto_create,
            open_time=open_time,
            site_url=self.site_url,
        )

    def run(self, reservation_data: dict[str, Any]) -> None:
        """Entry point invoked in background worker thread."""
        config = self._build_config(reservation_data)
        self.is_running = True
        self.stop_event.clear()

        try:
            asyncio.run(self._async_run(config))
        except Exception as e:
            msg = f"캐치테이블 엔진 실행 오류: {format_exception(e)}"
            self.log(msg, "red")
            self.emit_event(BookingEventType.ERROR, msg)
        finally:
            self.is_running = False

    async def _async_run(self, config: CatchTableBookingConfig) -> None:
        """Main async reservation pipeline."""
        self.log(f"[캐치테이블] 대상 매장 탐색 중: {config.shop_alias_or_ref}...", "cyan")
        self.emit_event(BookingEventType.INFO, f"대상 매장 탐색: {config.shop_alias_or_ref}")

        client = CatchTableClient(
            app_base=config.site_url,
            api_base=config.api_url,
            auth_token=config.auth_token,
            device_id=config.device_id,
            cookies=config.cookies,
        )
        self._active_client = client

        try:
            # 0. Session verification & login status
            user_name = config.user_name
            user_phone = config.user_phone
            user_email = config.user_email

            if config.use_login:
                self.log("[로그인 검증] 회원 세션 유효성 확인 중...", "cyan")
                validation = await client.validate_session()
                if validation.is_valid:
                    self.log(
                        f"[로그인 확인] {validation.user_name}님 계정 인증 성공 (연락처: {validation.user_phone})",
                        "green",
                    )
                    user_name = user_name or validation.user_name
                    user_phone = user_phone or validation.user_phone
                    user_email = user_email or validation.user_email
                else:
                    self.log(
                        f"[로그인 안내] {validation.error_message} 비회원 초고속 선점 모드로 계속 진행합니다.",
                        "yellow",
                    )
            else:
                self.log("[모드 설정] 비로그인(익명) 초고속 선점 모드로 동작합니다.", "cyan")

            # 1. Resolve shop details
            shop_info = await client.resolve_shop(config.shop_alias_or_ref)
            shop_ref = shop_info.get("shopRef") or config.shop_alias_or_ref
            shop_name = shop_info.get("shopName") or config.shop_alias_or_ref
            self.log(f"[매장 확인] {shop_name} (Ref: {shop_ref})", "green")

            # 2. Fetch RSA public key
            self.log("[보안키 획득] RSA 슬롯 암호화 키 요청 중...", "cyan")
            rsa_key = await client.fetch_rsa_key()
            self.log("[보안키 완료] RSA 공개키 및 위변조 방지 파라미터 획득 성공", "green")

            # 3. Wait for open time if specified
            if config.open_time:
                clock = ServerClock(config.api_url)
                self.log(f"[오픈 대기] 오픈 예정 시각: {config.open_time} (서버 시간 동기화 중)", "yellow")
                await self._wait_until_open_time(clock, config.open_time)

            # 4. Slot hunting loop (Hot Path)
            self.log(f"[슬롯 탐색] 날짜: {config.target_date}, 인원: {config.person_count}명, 우선순위: {config.time_priorities or '전체'}", "cyan")
            matched_slot = await self._hunt_matching_slot(client, shop_ref, config, rsa_key)

            if not matched_slot:
                if self.stop_event.is_set():
                    self.log("[중단] 사용자에 의해 탐색이 중단되었습니다.", "yellow")
                else:
                    self.log("[탐색 종료] 조건에 부합하는 예약 가능 슬롯을 찾지 못했습니다.", "red")
                return

            # 5. Acquire Holding Lock
            self.log(f"[선점 요청] {matched_slot.formatted_time} 슬롯 선점(Holding) 시도 중...", "cyan")
            holding = await client.request_holding(
                shop_ref=shop_ref,
                visit_yymmdd=matched_slot.date,
                visit_hhmi=matched_slot.time,
                person_count=config.person_count,
                table_type=matched_slot.table_type,
                menu_set_seqs=[matched_slot.menu_set_seq] if matched_slot.menu_set_seq else [],
            )
            self._holding_result = holding

            success_msg = f"[선점 성공] 슬롯 임시 선점 완료! (일련번호: {holding.holding_seq}, 시간: {matched_slot.formatted_time})"
            self.log(success_msg, "green")
            self.emit_event(
                BookingEventType.SUCCESS,
                success_msg,
                details={
                    "holding_seq": holding.holding_seq,
                    "time": matched_slot.formatted_time,
                    "date": matched_slot.date,
                    "deposit_required": holding.deposit_required,
                },
            )

            # 6. Finalize Reservation if auto_create enabled
            if config.auto_create and not holding.deposit_required:
                self.log("[최종 확정] 예약 생성 요청 중...", "cyan")
                create_res = await client.create_reservation(
                    holding_seq=holding.holding_seq,
                    user_name=user_name,
                    user_phone=user_phone,
                    user_email=user_email,
                )
                self.log(f"[예약 완료] 캐치테이블 예약이 최종 확정되었습니다! ({shop_name} {matched_slot.formatted_time})", "green")
            elif holding.deposit_required:
                self.log("[안내] 예약금 결제가 필요한 매장입니다. 캐치테이블 앱/웹에서 선점된 슬롯의 결제를 완료해 주세요.", "yellow")

            # Fire success callback
            if self.success_callback:
                self.success_callback()

        finally:
            await client.close()

    async def _wait_until_open_time(self, clock: ServerClock, open_time_str: str) -> None:
        """Countdown until target open time with millisecond precision."""
        # Simple wait loop until server time reaches open time
        while not self.stop_event.is_set():
            now = clock.now()
            target_today = now.strftime("%Y-%m-%d") + " " + open_time_str
            # If within 500ms of open time, enter fast poll
            time_diff = (
                time.mktime(time.strptime(target_today, "%Y-%m-%d %H:%M:%S"))
                - now.timestamp()
            )
            if time_diff <= 0.2:
                break
            if time_diff > 2.0:
                await asyncio.sleep(1.0)
            else:
                await asyncio.sleep(0.05)

    async def _hunt_matching_slot(
        self,
        client: CatchTableClient,
        shop_ref: str,
        config: CatchTableBookingConfig,
        rsa_key: CatchTableRsaKey,
    ) -> CatchTableTimeSlot | None:
        """Poll and evaluate time slots against user priority ladder."""
        attempts = 0
        while not self.stop_event.is_set():
            attempts += 1
            with self._lock:
                self._attempt_count = attempts

            try:
                slots = await client.get_time_slots(
                    shop_ref=shop_ref,
                    search_date=config.target_date,
                    person_count=config.person_count,
                    table_type=config.table_type,
                    rsa_key=rsa_key,
                )

                available_slots = [s for s in slots if s.available_yn]
                if available_slots:
                    # Match against priorities
                    if config.time_priorities:
                        for prio in config.time_priorities:
                            clean_prio = prio.replace(":", "")
                            for slot in available_slots:
                                if slot.time.replace(":", "") == clean_prio:
                                    return slot
                    else:
                        # Return first available slot
                        return available_slots[0]

            except Exception as e:
                logger.debug("Slot poll error on attempt %d: %s", attempts, e)

            await asyncio.sleep(self.HOT_PATH_INTERVAL)

        return None

    def stop(self) -> None:
        """Stop the running engine immediately."""
        self.stop_event.set()
        self.is_running = False
        self.log("[정지] 캐치테이블 예약 감시를 중단합니다.", "yellow")
