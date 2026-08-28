from __future__ import annotations

import asyncio
import hashlib
import html
import re
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

from engines.async_hot_path import create_isolated_session, create_shared_connector
from engines.base_engine import BaseEngine
from engines.zeroworld_catalog import (
    ZeroWorldTimeSlot,
    calendar_contains_date,
    decode_body,
    find_target_time_slot,
    subject_for_branch,
)
from pengucro.diagnostics import format_exception, write_redacted_debug_text
from pengucro.models import BookingResult
from pengucro.storage import append_history, data_path


@dataclass(frozen=True)
class ZeroWorldContext:
    branch: str
    subject: str
    reservation_date: str
    theme: str
    target_time: str
    name: str
    phone: str
    people: str


class ZeroWorldShinEngine(BaseEngine):
    """Reservation adapter for the current Sinbiweb ZeroWorld site."""

    USE_ASYNC_HOT_PATH = True
    LOOKUP_TIMEOUT_SECONDS = 8.0
    SUBMIT_TIMEOUT_SECONDS = 12.0

    SELECT_URL = "https://zeroworldkorea.com/core/res/rev.make.sel.php"
    ACTION_URL = "https://zeroworldkorea.com/core/res/rev.act.php"
    PAYMENT_URL = "https://zeroworldkorea.com/core/res/rev.make.mutong.php"
    HOME_URL = "https://zeroworldkorea.com/layout/res/home.php?go=main"
    SUPPORTED_BRANCHES = {"1", "2", "4", "5"}

    def __init__(self, site_url: str, log_callback, success_callback=None, engine_options=None):
        super().__init__(log_callback, success_callback)
        options = dict(engine_options or {})
        self.site_url = site_url or options.get("home_url") or self.HOME_URL
        parsed = urllib.parse.urlparse(self.site_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "https://zeroworldkorea.com"
        self.home_url = options.get("home_url") or urllib.parse.urljoin(base_url, "/layout/res/home.php")
        self.select_url = options.get("select_url") or urllib.parse.urljoin(base_url, "/core/res/rev.make.sel.php")
        self.action_url = options.get("action_url") or urllib.parse.urljoin(base_url, "/core/res/rev.act.php")
        self.payment_url = options.get("payment_url") or urllib.parse.urljoin(base_url, "/core/res/rev.make.mutong.php")
        self.subject_by_branch = {
            str(key): str(value) for key, value in options.get("subject_by_branch", {}).items()
        }
        self.supported_branches = set(self.subject_by_branch) or set(self.SUPPORTED_BRANCHES)
        self._last_messages: dict[str, float] = {}
        self._slot_lookup_key: tuple[str, str, str] | None = None
        self._slot_lookup_payload: dict[str, str] = {}

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return max(0.0, (time.perf_counter() - started) * 1000.0)

    @staticmethod
    def _safe_text(value: Any, limit: int = 240, extra_secrets=()) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        for secret in sorted(
            {str(item).strip() for item in extra_secrets if str(item).strip()},
            key=len,
            reverse=True,
        ):
            if len(secret) >= 2:
                text = text.replace(secret, "[숨김]")
        text = re.sub(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", "[이메일 숨김]", text)
        text = re.sub(r"(?<!\d)01[016789][ -]?\d{3,4}[ -]?\d{4}(?!\d)", "[전화번호 숨김]", text)
        text = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[긴 숫자 숨김]", text)
        text = re.sub(
            r"(?i)\b(token|csrf|cookie|session|name|phone|mobile|account|amount|code|ck_code)"
            r"\s*[=:]\s*[^\s,;&]+",
            lambda match: f"{match.group(1)}=[숨김]",
            text,
        )
        return text[:limit]

    @classmethod
    def _format_exception(cls, exc: BaseException, context: ZeroWorldContext | None = None) -> str:
        secrets = (context.name, context.phone) if context else ()
        return cls._safe_text(format_exception(exc), extra_secrets=secrets)

    def _log_http(
        self,
        worker_name: str,
        stage: str,
        status: int,
        rtt_ms: float,
        suffix: str = "",
        log_type: str = "info",
    ) -> None:
        detail = f" · {suffix}" if suffix else ""
        self.log(
            f"[{worker_name}] {stage} 응답 · HTTP {status} · RTT {rtt_ms:.0f}ms{detail}",
            log_type,
        )

    @staticmethod
    def _format_phone(phone: str) -> str:
        digits = "".join(character for character in phone if character.isdigit())
        if len(digits) == 11:
            return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return digits

    def _build_context(self, reservation_data: dict[str, Any]) -> ZeroWorldContext:
        branch = str(reservation_data.get("branch", "1"))
        if branch not in self.supported_branches:
            raise ValueError("지원하지 않는 제로월드 지점입니다. 김포·강남·홍대·다이브 건대만 선택할 수 있습니다.")
        return ZeroWorldContext(
            branch=branch,
            subject=self.subject_by_branch.get(branch, subject_for_branch(branch)),
            reservation_date=str(reservation_data.get("reservationDate", "")),
            theme=str(reservation_data.get("themePK", "")),
            target_time=str(reservation_data.get("reservationTime", ""))[:5],
            name=str(reservation_data.get("name", "")),
            phone=self._format_phone(str(reservation_data.get("phone", ""))),
            people=str(reservation_data.get("people", "2")),
        )

    def _headers(self, context: ZeroWorldContext) -> dict[str, str]:
        referer = (
            f"{self.home_url}?go=rev.make"
            f"&s_subj={context.subject}&zizum_num={context.branch}&rev_days={context.reservation_date}"
        )
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": referer,
        }

    def _log_throttled(self, key: str, message: str, log_type: str = "warning", interval: float = 2.0) -> None:
        now = time.monotonic()
        if now - self._last_messages.get(key, 0.0) >= interval:
            self._last_messages[key] = now
            self.log(message, log_type)

    def make_reservation_thread(self, reservation_data: dict[str, Any]) -> None:
        asyncio.run(self.make_reservation_async_task(reservation_data, 0))

    async def pre_fetch_sessions_async(
        self,
        num_sessions: int,
        reservation_data: dict[str, Any],
    ) -> None:
        """Warm isolated booking sessions over one shared DNS/TLS connector."""

        context = self._build_context(reservation_data)
        self.session_pool = []
        self._shared_connector = create_shared_connector(num_sessions)
        timeout = aiohttp.ClientTimeout(total=self.LOOKUP_TIMEOUT_SECONDS)
        self.log(f"제로월드 연결·예약 단계 예열 시작 · 세션 {num_sessions}개", "info")

        async def prepare_one(index: int):
            session = create_isolated_session(
                self._shared_connector,
                headers=self._headers(context),
                timeout=timeout,
            )
            try:
                await self.wait_async_scan_turn()
                prepared = await self._prestage_session(
                    session,
                    context,
                    f"예열 {index + 1}",
                )
                return session, prepared, ""
            except Exception as exc:
                self.log(
                    f"[예열 {index + 1}] 예약 단계 예열 실패 · "
                    f"{self._format_exception(exc, context)} · 실행 중 자동 복구",
                    "warning",
                )
                return session, False, ""

        self.session_pool = list(
            await asyncio.gather(*(prepare_one(index) for index in range(num_sessions)))
        )
        prepared_count = sum(1 for _session, prepared, _slot in self.session_pool if prepared)
        self.log(
            f"제로월드 연결·예약 단계 예열 완료 · 준비 {prepared_count}/{num_sessions} · "
            "전체 세션 연속 스캔 시작",
            "info",
        )

    async def make_reservation_async_task(self, reservation_data: dict[str, Any], task_idx: int) -> None:
        try:
            context = self._build_context(reservation_data)
        except ValueError as exc:
            self.log(str(exc), "error")
            self.stop_event.set()
            return

        worker_name = f"작업 {task_idx + 1}"
        self.log(
            f"[{worker_name}] 신 제로월드 감시 시작 · 지점 {context.branch} · 테마 {context.theme} · "
            f"{context.reservation_date} {context.target_time}",
            "info",
        )
        pooled = bool(getattr(self, "session_pool", []))
        if pooled:
            session, session_prepared, preselected_slot_id = self.session_pool[
                task_idx % len(self.session_pool)
            ]
        else:
            session = aiohttp.ClientSession(
                headers=self._headers(context),
                timeout=aiohttp.ClientTimeout(total=self.LOOKUP_TIMEOUT_SECONDS),
            )
            session_prepared = False
            preselected_slot_id = ""
        try:
            while not self.stop_event.is_set():
                stage = "예약 단계 사전 준비"
                try:
                    await self.wait_async_scan_turn()
                    if self.stop_event.is_set():
                        return
                    if not session_prepared:
                        session_prepared = await self._prestage_session(
                            session, context, worker_name
                        )
                        if not session_prepared:
                            continue

                    stage = "슬롯 조회"
                    target_slot = await self._find_target_slot(
                        session, context, worker_name
                    )
                    if target_slot is None:
                        self.silent_tick(f"{context.target_time} 슬롯 대기")
                        continue

                    if not target_slot.available:
                        if (
                            target_slot.slot_id
                            and target_slot.slot_id != preselected_slot_id
                        ):
                            stage = "시간 선택 사전 준비"
                            if await self._prepare_time_slot(
                                session,
                                target_slot.slot_id,
                                worker_name,
                                "시간 선택 사전 준비",
                            ):
                                preselected_slot_id = target_slot.slot_id
                        self.silent_tick(f"{context.target_time} 슬롯 대기")
                        continue

                    slot_id = target_slot.slot_id

                    stage = "예약 제출"
                    async with self.async_submission_lock:
                        if self.stop_event.is_set():
                            return
                        if preselected_slot_id != slot_id:
                            stage = "시간 선택 준비"
                            if not await self._prepare_time_slot(
                                session, slot_id, worker_name, "시간 선택 준비"
                            ):
                                continue
                            preselected_slot_id = slot_id
                        stage = "예약 제출"
                        result = await self._submit(session, context, slot_id, worker_name)
                    if result:
                        self.log(f"[{worker_name}] 🎉 {result.message}", "success")
                        self.notify_success(result)
                        return
                    # A pre-open time selection can be accepted at the HTTP
                    # layer without becoming active server-side.  Refresh it on
                    # the next attempt only when the fast-path submission fails.
                    session_prepared = False
                    preselected_slot_id = ""
                    await asyncio.sleep(0.4)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    recovery_delay = self.observe_async_network_failure()
                    self._log_throttled(
                        f"network:{stage}",
                        f"[{worker_name}] {stage} 통신 오류 · {self._format_exception(exc, context)} · "
                        f"복구 간격 {max(0.5, recovery_delay):.1f}초 후 재시도",
                    )
                    await asyncio.sleep(max(0.5, recovery_delay))
                except Exception as exc:
                    recovery_delay = self.observe_async_network_failure()
                    self._log_throttled(
                        f"unexpected:{stage}",
                        f"[{worker_name}] {stage} 처리 오류 · {self._format_exception(exc, context)} · "
                        f"복구 간격 {max(0.5, recovery_delay):.1f}초 후 재시도",
                        "error",
                    )
                    await asyncio.sleep(max(0.5, recovery_delay))
        finally:
            if not pooled:
                await session.close()

    async def _wait_for_date(
        self,
        session: aiohttp.ClientSession,
        context: ZeroWorldContext,
        worker_name: str = "작업 1",
    ) -> bool:
        year, month, _ = context.reservation_date.split("-")
        payload = {
            "act": "calendar",
            "zizum_num": context.branch,
            "rev_days": context.reservation_date,
            "year": year,
            "month": month,
            "s_subj": context.subject,
        }
        started = time.perf_counter()
        async with session.post(self.select_url, data=payload) as response:
            body = decode_body(await response.read())
            status = response.status
        rtt_ms = self._elapsed_ms(started)
        if calendar_contains_date(body, context.reservation_date):
            self._log_http(
                worker_name,
                "날짜 조회",
                status,
                rtt_ms,
                f"{context.reservation_date} 오픈 확인",
            )
            return True
        self.silent_tick(f"{context.reservation_date} 날짜 대기")
        retry_reason = (
            "서버 응답 오류 · 재시도"
            if status != 200
            else f"{context.reservation_date} 미공개 · 재시도"
        )
        self._log_throttled(
            f"date:{context.reservation_date}:{status}",
            f"[{worker_name}] 날짜 조회 응답 · HTTP {status} · RTT {rtt_ms:.0f}ms · "
            f"{retry_reason}",
            "warning" if status != 200 else "info",
        )
        return False

    async def _find_slot(
        self,
        session: aiohttp.ClientSession,
        context: ZeroWorldContext,
        worker_name: str = "작업 1",
    ) -> str:
        target_slot = await self._find_target_slot(session, context, worker_name)
        if target_slot is not None and target_slot.available:
            return target_slot.slot_id
        return ""

    async def _find_target_slot(
        self,
        session: aiohttp.ClientSession,
        context: ZeroWorldContext,
        worker_name: str = "작업 1",
    ) -> ZeroWorldTimeSlot | None:
        lookup_key = (context.branch, context.reservation_date, context.theme)
        if lookup_key != self._slot_lookup_key:
            self._slot_lookup_key = lookup_key
            self._slot_lookup_payload = {
                "act": "theme_time_list",
                "zizum_num": context.branch,
                "rev_days": context.reservation_date,
                "theme_num": context.theme,
            }
        started = time.perf_counter()
        async with session.post(self.select_url, data=self._slot_lookup_payload) as response:
            status = response.status
            if response.status != 200:
                await response.read()
                rtt_ms = self._elapsed_ms(started)
                recovery_delay = self.observe_async_response(response, rtt_ms)
                recovery_detail = (
                    f" · 서버 복구 간격 {recovery_delay:.1f}초"
                    if recovery_delay
                    else ""
                )
                self._log_throttled(
                    f"slot-http:{status}",
                    f"[{worker_name}] 슬롯 조회 응답 · HTTP {status} · RTT {rtt_ms:.0f}ms · "
                    f"서버 응답 오류{recovery_detail}로 재시도",
                    "warning",
                )
                return None
            body = decode_body(await response.read())
        rtt_ms = self._elapsed_ms(started)
        recovery_delay = self.observe_async_response(response, rtt_ms)
        slot, slot_count = find_target_time_slot(body, context.target_time)
        if slot is not None:
            if slot.available:
                self._log_http(
                    worker_name,
                    "슬롯 조회",
                    status,
                    rtt_ms,
                    f"조회 {slot_count}개 · {context.target_time} 발견 · 슬롯 ID {slot.slot_id}",
                )
            else:
                slot_detail = f" · 슬롯 ID {slot.slot_id}" if slot.slot_id else ""
                self._log_throttled(
                    f"slot:{context.target_time}",
                    f"[{worker_name}] 슬롯 조회 응답 · HTTP {status} · RTT {rtt_ms:.0f}ms · "
                    f"조회 {slot_count}개 · {context.target_time} 예약불가{slot_detail} · 재시도",
                    "info",
                )
            return slot
        recovery_detail = f" · 서버 복구 간격 {recovery_delay:.1f}초" if recovery_delay else ""
        self._log_throttled(
            f"slot:{context.target_time}",
            f"[{worker_name}] 슬롯 조회 응답 · HTTP {status} · RTT {rtt_ms:.0f}ms · "
            f"조회 {slot_count}개 · {context.target_time} 미공개 또는 예약불가"
            f"{recovery_detail} · 재시도",
            "info",
        )
        return None

    async def _prestage_session(
        self,
        session: aiohttp.ClientSession,
        context: ZeroWorldContext,
        worker_name: str = "작업 1",
    ) -> bool:
        theme_list_ready = await self._post_and_discard(
            session,
            {
                "act": "theme_list",
                "zizum_num": context.branch,
                "rev_days": context.reservation_date,
                "theme_num": "",
                "s_subj": context.subject,
            },
            worker_name,
            "테마 목록 사전 준비",
        )
        theme_ready = await self._post_and_discard(
            session,
            {
                "act": "theme_select",
                "theme_num": context.theme,
                "rev_days": context.reservation_date,
                "theme_time_num": "",
            },
            worker_name,
            "테마 선택 사전 준비",
        )
        return theme_list_ready and theme_ready

    async def _prepare_time_slot(
        self,
        session: aiohttp.ClientSession,
        slot_id: str,
        worker_name: str = "작업 1",
        stage: str = "시간 선택 준비",
    ) -> bool:
        return await self._post_and_discard(
            session,
            {"act": "theme_time_select", "theme_time_num": slot_id},
            worker_name,
            stage,
        )

    async def _submit(
        self,
        session: aiohttp.ClientSession,
        context: ZeroWorldContext,
        slot_id: str,
        worker_name: str = "작업 1",
    ) -> BookingResult | None:
        action_data = {
            "name": context.name,
            "mobile": context.phone,
            "person": context.people,
            "zizum_num": context.branch,
            "rev_days": context.reservation_date,
            "theme_num": context.theme,
            "theme_time_num": slot_id,
            "act": "make",
            "s_subj": context.subject,
        }
        started = time.perf_counter()
        async with session.post(
            self.action_url,
            data=action_data,
            timeout=aiohttp.ClientTimeout(total=self.SUBMIT_TIMEOUT_SECONDS),
        ) as response:
            body = decode_body(await response.read())
            status = response.status
            final_url = str(response.url)
            history_urls = [str(item.url) for item in response.history]
        self._log_http(
            worker_name,
            "예약 제출",
            status,
            self._elapsed_ms(started),
            f"슬롯 ID {slot_id} · 리디렉션 {len(history_urls)}회",
        )

        if not self._submission_accepted(body, final_url, history_urls):
            error = self._safe_text(
                self._extract_alert(body) or "예약 제출 승인 표식을 찾지 못했습니다.",
                extra_secrets=(context.name, context.phone),
            )
            self._log_throttled(
                f"submit:{error}",
                f"[{worker_name}] 예약 제출 거절 또는 미승인 · HTTP {status} · 슬롯 ID {slot_id} · "
                f"사유 {error} · 재시도",
            )
            return None

        self.log(
            f"[{worker_name}] 예약 제출 승인 경로 확인 · 슬롯 ID {slot_id} · "
            "결제/완료 단계로 진행",
            "info",
        )
        return await self._complete_payment(
            session,
            body,
            final_url,
            history_urls,
            context,
            worker_name,
        )

    async def _post_and_discard(
        self,
        session: aiohttp.ClientSession,
        data: dict[str, str],
        worker_name: str,
        stage: str,
    ) -> bool:
        started = time.perf_counter()
        async with session.post(self.select_url, data=data) as response:
            await response.read()
            status = response.status
            recovery_delay = self.observe_async_response(
                response,
                self._elapsed_ms(started),
            )
        succeeded = 200 <= status < 300
        self._log_http(
            worker_name,
            stage,
            status,
            self._elapsed_ms(started),
            ""
            if succeeded
            else (
                "사전 준비 실패"
                f"{f' · 서버 복구 간격 {recovery_delay:.1f}초' if recovery_delay else ''}"
                " · 재시도"
            ),
            "info" if succeeded else "warning",
        )
        return succeeded

    @staticmethod
    def _submission_accepted(body: str, final_url: str, history_urls: list[str]) -> bool:
        combined = " ".join([body, final_url, *history_urls]).lower()
        failure_alert = ZeroWorldShinEngine._extract_alert(body)
        if failure_alert and not any(word in failure_alert for word in ("완료", "성공")):
            return False
        markers = ("rev.pay", "rev.kcp", "rev.make.mutong", "toss", "vbank")
        return any(marker in combined for marker in markers) or (
            "code=" in combined and any(word in combined for word in ("결제", "payment", "예약확인"))
        )

    @staticmethod
    def _extract_alert(body: str) -> str:
        match = re.search(r"alert\s*\(\s*['\"](.*?)['\"]\s*\)", body, re.S)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
        soup = BeautifulSoup(body, "html.parser")
        for script in soup.find_all("script"):
            match = re.search(r"alert\s*\(\s*['\"](.*?)['\"]\s*\)", script.get_text(), re.S)
            if match:
                return re.sub(r"\s+", " ", match.group(1)).strip()
        return ""

    async def _complete_payment(
        self,
        session: aiohttp.ClientSession,
        body: str,
        final_url: str,
        history_urls: list[str],
        context: ZeroWorldContext,
        worker_name: str = "작업 1",
    ) -> BookingResult:
        combined = " ".join([body, final_url, *history_urls])
        reservation_code = self._extract_value(body, "code")
        check_code = self._extract_value(body, "ck_code")
        if not reservation_code:
            match = re.search(r"[?&]code=([A-Za-z0-9_-]+)", combined)
            reservation_code = match.group(1) if match else ""
        if not reservation_code:
            debug_path = self._save_debug("zeroworld_submit_debug.html", body, "예약 제출")
            self.log(
                f"[{worker_name}] 예약 코드 미확인 · 제출 승인 경로 근거로 선점 판정 유지 · "
                f"민감정보 제외 진단 요약 저장{f' ({debug_path.name})' if debug_path else ''}",
                "warning",
            )
            return BookingResult(True, "예약 선점 성공 · 사이트에서 예약 내역을 확인해주세요.")

        if not check_code:
            kcp_url = f"{self.home_url}?go=rev.kcp&code={urllib.parse.quote(reservation_code)}"
            started = time.perf_counter()
            async with session.get(
                kcp_url,
                timeout=aiohttp.ClientTimeout(total=self.SUBMIT_TIMEOUT_SECONDS),
            ) as response:
                check_code = self._extract_value(decode_body(await response.read()), "ck_code")
                status = response.status
            self._log_http(
                worker_name,
                "결제 확인 정보 조회",
                status,
                self._elapsed_ms(started),
                "확인값 존재" if check_code else "확인값 없음",
            )

        payment_data = {
            "code": reservation_code,
            "ck_code": check_code,
            "layout_folder": "layout/res",
            "payment": "A",
            "privacy": "on",
            "name": context.name,
            "mobile": context.phone,
            "tel": context.phone,
        }
        started = time.perf_counter()
        async with session.post(
            self.payment_url,
            data=payment_data,
            timeout=aiohttp.ClientTimeout(total=self.SUBMIT_TIMEOUT_SECONDS),
        ) as response:
            payment_body = decode_body(await response.read())
            payment_status = response.status
        self._log_http(
            worker_name,
            "결제/접수 완료 요청",
            payment_status,
            self._elapsed_ms(started),
        )

        refresh = re.search(r"url=([^'\">]+)", payment_body, re.I)
        if refresh:
            next_url = urllib.parse.urljoin(self.payment_url, refresh.group(1).strip())
            started = time.perf_counter()
            async with session.get(
                next_url,
                timeout=aiohttp.ClientTimeout(total=self.SUBMIT_TIMEOUT_SECONDS),
            ) as response:
                payment_body += decode_body(await response.read())
                refresh_status = response.status
            self._log_http(
                worker_name,
                "완료 화면 확인",
                refresh_status,
                self._elapsed_ms(started),
            )

        number_match = re.search(r"ck_code=(\d+)", payment_body)
        if not number_match:
            number_match = re.search(r"예약번호[^0-9]*(\d+)", payment_body)
        booking_number = number_match.group(1) if number_match else check_code
        completed = any(
            marker in payment_body
            for marker in ("rev.make.exe.php", "rev.make.end", "완료", "접수", "성공")
        )
        if completed:
            self.log(
                f"[{worker_name}] 예약 완료 표식 확인 · "
                f"예약번호 {booking_number or '확인 필요'}",
                "info",
            )
            append_history(
                {
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "site": "제로월드",
                    "branch": context.branch,
                    "date": context.reservation_date,
                    "time": context.target_time,
                    "booking_number": booking_number,
                }
            )
            try:
                webbrowser.open(
                    f"{self.home_url}?go=rev.make.end&code={urllib.parse.quote(reservation_code)}"
                )
            except OSError:
                pass
            return BookingResult(
                True,
                f"예약 최종 완료 · 예약번호 {booking_number or '확인 필요'}",
                booking_number=booking_number,
                details={"reservation_code": reservation_code},
            )

        debug_path = self._save_debug("zeroworld_payment_debug.html", payment_body, "결제/접수 완료")
        self.log(
            f"[{worker_name}] 예약 완료 표식 미확인 · 앞 단계 승인 근거로 선점 판정 유지 · "
            f"민감정보 제외 진단 요약 저장{f' ({debug_path.name})' if debug_path else ''}",
            "warning",
        )
        return BookingResult(
            True,
            f"예약 선점 성공 · 예약번호 {booking_number or '확인 필요'} · 사이트 확인 필요",
            booking_number=booking_number,
            details={"reservation_code": reservation_code},
        )

    @staticmethod
    def _extract_value(body: str, name: str) -> str:
        pattern = rf"name=['\"]?{re.escape(name)}['\"]?\s*value=['\"]?([^'\"'>\s]+)"
        match = re.search(pattern, body, re.I)
        return match.group(1) if match else ""

    @classmethod
    def _save_debug(cls, filename: str, body: str, stage: str = "응답 분석"):
        """Persist structure-only diagnostics; never retain the raw response body."""

        try:
            soup = BeautifulSoup(body, "html.parser")
            title_text = soup.title.get_text(" ", strip=True) if soup.title else ""
            alert_count = len(re.findall(r"alert\s*\(", body, re.I))
            markers = [
                marker
                for marker in (
                    "rev.pay",
                    "rev.kcp",
                    "rev.make.mutong",
                    "rev.make.exe.php",
                    "rev.make.end",
                    "toss",
                    "vbank",
                )
                if marker.lower() in body.lower()
            ]
            digest = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()
            marker_items = "".join(f"<li>{html.escape(marker)}</li>" for marker in markers)
            summary = (
                "<!doctype html><html lang='ko'><meta charset='utf-8'>"
                "<title>제로월드 안전 진단 요약</title><body>"
                "<h1>제로월드 안전 진단 요약</h1>"
                f"<p>단계: {html.escape(stage)}</p>"
                f"<p>응답 길이: {len(body)}자</p>"
                f"<p>SHA-256: {digest}</p>"
                f"<p>문서 제목 존재: {'예' if title_text else '아니오'}"
                f" · 길이 {len(title_text)}자</p>"
                f"<p>폼 개수: {len(soup.find_all('form'))}</p>"
                f"<p>alert 호출 수: {alert_count}</p>"
                f"<h2>흐름 표식</h2><ul>{marker_items or '<li>(없음)</li>'}</ul>"
                "<p>개인정보, hidden 값, 스크립트 본문과 원문 응답은 저장하지 않았습니다.</p>"
                "</body></html>"
            )
            target = data_path(filename)
            return write_redacted_debug_text(target, summary)
        except OSError:
            return None
