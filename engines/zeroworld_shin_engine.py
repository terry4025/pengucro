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
from engines.zeroworld_captcha import recognize_digits, parse_digest, warm_ocr
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
    theme_name: str = ""


class ZeroWorldAuthenticationRequired(RuntimeError):
    pass


class ZeroWorldShinEngine(BaseEngine):
    """Reservation adapter for the current Sinbiweb ZeroWorld site."""

    USE_ASYNC_HOT_PATH = True
    LOOKUP_TIMEOUT_SECONDS = 8.0
    SUBMIT_TIMEOUT_SECONDS = 12.0
    SUBMIT_RECONCILE_SECONDS = 3.0

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
        self._final_submission_state = "idle"
        self._captcha_values: dict[int, tuple[str, float]] = {}
        self._published_dates: set[tuple[str, str]] = set()
        self._known_booking_number = ""

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
            theme_name=str(reservation_data.get("theme_name") or reservation_data.get("theme_label") or reservation_data.get("themeLabel") or ""),
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
        self._final_submission_state = "idle"
        self._published_dates.clear()
        self._captcha_values.clear()
        self._known_booking_number = ""
        try:
            await asyncio.to_thread(warm_ocr)
        except Exception:
            self.stop_event.set()
            self.log("숫자 OCR 초기화 실패 · 예약을 시작하지 않았습니다.", "error")
            return
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
                    date_key = (context.branch, context.reservation_date)
                    if date_key not in self._published_dates:
                        stage = "날짜 공개 확인"
                        if not await self._wait_for_date(session, context, worker_name):
                            continue
                        self._published_dates.add(date_key)
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
                        if self.stop_event.is_set() or self._final_submission_state in {
                            "success", "uncertain",
                        }:
                            return
                        if preselected_slot_id != slot_id:
                            stage = "시간 선택 준비"
                            if not await self._prepare_time_slot(
                                session, slot_id, worker_name, "시간 선택 준비"
                            ):
                                continue
                            preselected_slot_id = slot_id
                        stage = "예약 제출"
                        self._final_submission_state = "inflight"
                        try:
                            result = await self._submit(
                                session, context, slot_id, worker_name
                            )
                        except ZeroWorldAuthenticationRequired as exc:
                            self._final_submission_state = "authentication_required"
                            self.stop_event.set()
                            self.log(f"[{worker_name}] 인증 필요 · {exc} · 추가 예약 제출 중지", "error")
                            return
                        except Exception as exc:
                            result = await self._reconcile_ambiguous_submit(
                                session, context, worker_name
                            )
                            if result is None:
                                self._final_submission_state = "uncertain"
                                self.stop_event.set()
                                self.log(
                                    f"[{worker_name}] [중복 방지 정지] 예약 POST 뒤 응답을 "
                                    "확정하지 못했습니다. 추가 제출 없이 사이트 예약내역을 "
                                    f"확인해주세요. ({self._format_exception(exc, context)})",
                                    "error",
                                )
                                return
                        if result is not None:
                            self._final_submission_state = "success" if result.success else "uncertain"
                            self.stop_event.set()
                    if result:
                        self.log(f"[{worker_name}] {result.message}", "success" if result.success else "error")
                        if result.success:
                            self.notify_success(result)
                        return
                    self._final_submission_state = "idle"
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
        if status == 200 and calendar_contains_date(body, context.reservation_date):
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

    async def _prepare_captcha(self, session, worker_name: str) -> str:
        cached = self._captcha_values.get(id(session))
        if cached and time.monotonic() - cached[1] < 60:
            return cached[0]
        origin = urllib.parse.urljoin(self.home_url, "/core/captcha/")
        for attempt in range(3):
            if self.stop_event.is_set():
                raise ZeroWorldAuthenticationRequired("작업 중지")
            async with session.post(origin + "session.php", data={}, timeout=aiohttp.ClientTimeout(total=8)) as response:
                digest = parse_digest(decode_body(await response.read()))
                if response.status != 200 or not digest:
                    raise ZeroWorldAuthenticationRequired("인증 세션 응답 확인 불가")
            async with session.get(origin + f"image.php?t={time.time_ns()}", timeout=aiohttp.ClientTimeout(total=8)) as response:
                image_bytes = await response.read()
                if response.status != 200:
                    raise ZeroWorldAuthenticationRequired("인증 이미지 조회 실패")
            try:
                digits = await recognize_digits(image_bytes, digest)
            except Exception as exc:
                raise ZeroWorldAuthenticationRequired("숫자 OCR 사용 불가 또는 인증 구조 변경") from exc
            if digits:
                self._captcha_values[id(session)] = (digits, time.monotonic())
                self.log(f"[{worker_name}] 숫자 인증 OCR 검증 완료 · 동일 세션 · 시도 {attempt + 1}/3", "info")
                return digits
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
        raise ZeroWorldAuthenticationRequired("3회 제한 내 숫자 판독 실패")

    @staticmethod
    def _uncertain_result(reason: str) -> BookingResult:
        return BookingResult(False, f"예약 접수 여부 확인 필요 · {reason} · 추가 제출 없음",
                             details={"outcome": "uncertain"})

    async def _submit(
        self,
        session: aiohttp.ClientSession,
        context: ZeroWorldContext,
        slot_id: str,
        worker_name: str = "작업 1",
    ) -> BookingResult | None:
        captcha = await self._prepare_captcha(session, worker_name)
        if self.stop_event.is_set():
            raise ZeroWorldAuthenticationRequired("작업 중지")
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
            "input_captcha": captcha,
        }
        self._captcha_values.pop(id(session), None)
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

        if status != 200 or not self._submission_accepted(body, final_url, history_urls):
            alert = self._extract_alert(body)
            if "자동등록" in alert or "captcha" in alert.lower():
                raise ZeroWorldAuthenticationRequired("사이트가 인증값을 거절했습니다")
            if status != 200 or not alert:
                return self._uncertain_result("예약 응답에 명확한 승인·거절 근거 없음")
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
        try:
            return await self._complete_payment(
                session,
                body,
                final_url,
                history_urls,
                context,
                worker_name,
            )
        except Exception as exc:
            # The mutation was accepted before this follow-up began. A payment or
            # completion timeout must never turn into a second reservation POST.
            self.log(
                f"[{worker_name}] 예약 선점 승인 후 완료 단계 응답 지연 · "
                "추가 예약 POST 없이 사이트 확인 필요 · "
                f"{self._format_exception(exc, context)}",
                "warning",
            )
            if self._known_booking_number:
                return await self._confirm_booking(session, context, self._known_booking_number)
            return self._uncertain_result("승인 이후 완료 단계 응답 확인 실패")

    async def _reconcile_ambiguous_submit(
        self,
        session: aiohttp.ClientSession,
        context: ZeroWorldContext,
        worker_name: str,
    ) -> BookingResult | None:
        """Read state after ambiguity without another reservation/payment POST."""

        if self._known_booking_number:
            return await self._confirm_booking(session, context, self._known_booking_number)
        reconcile_url = f"{self.home_url}?go=rev.kcp"
        try:
            async with session.get(
                reconcile_url,
                timeout=aiohttp.ClientTimeout(total=self.SUBMIT_RECONCILE_SECONDS),
            ) as response:
                body = decode_body(await response.read())
                status = response.status
                final_url = str(response.url)
                history_urls = [str(item.url) for item in response.history]
            self._log_http(
                worker_name,
                "예약 결과 재확인",
                status,
                0.0,
                "추가 예약 POST 없음",
            )
            combined = " ".join([body, final_url, *history_urls])
            reservation_code = self._extract_value(body, "code")
            check_code = self._extract_value(body, "ck_code")
            if not reservation_code:
                match = re.search(r"[?&]code=([A-Za-z0-9_-]+)", combined)
                reservation_code = match.group(1) if match else ""
            if not reservation_code and not check_code:
                return None
            if not self._submission_accepted(body, final_url, history_urls):
                return None
            # Reconciliation is read-only. Never replay payment after an
            # exception that may itself have happened after payment submission.
            return self._receipt_result(body, context)
        except Exception:
            return None

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
        if failure_alert and (any(word in failure_alert for word in ("불가", "실패", "잘못", "취소", "환불", "이미"))
                              or not any(word in failure_alert for word in ("완료", "성공"))):
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
                f"[{worker_name}] 예약 코드 미확인 · 성공 판정 보류 · "
                f"민감정보 제외 진단 요약 저장{f' ({debug_path.name})' if debug_path else ''}",
                "warning",
            )
            return self._uncertain_result("예약 코드 미확인")

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
            if status != 200:
                return self._uncertain_result("결제 확인 정보 조회 오류")

        if not check_code:
            return self._uncertain_result("결제 확인값 미확인")
        self._known_booking_number = check_code
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
                payment_body = decode_body(await response.read())
                refresh_status = response.status
            self._log_http(
                worker_name,
                "완료 화면 확인",
                refresh_status,
                self._elapsed_ms(started),
            )

        receipt = (self._receipt_result(payment_body, context)
                   if payment_status == 200 and (not refresh or refresh_status == 200)
                   else self._uncertain_result("결제/완료 응답 오류"))
        if not receipt.success:
            # One read-only receipt fetch; never repeat the payment POST.
            end_url = f"{self.home_url}?go=rev.make.end&code={urllib.parse.quote(reservation_code)}"
            async with session.get(end_url, timeout=aiohttp.ClientTimeout(total=self.SUBMIT_TIMEOUT_SECONDS)) as response:
                end_body = decode_body(await response.read())
                if response.status == 200:
                    receipt = self._receipt_result(end_body, context)
        if not receipt.success:
            receipt = await self._confirm_booking(session, context, check_code)
        booking_number = receipt.booking_number
        if receipt.success:
            self.log(
                f"[{worker_name}] 예약 완료 표식 확인 · "
                f"예약번호 {booking_number or '확인 필요'}",
                "info",
            )
            try:
                append_history({
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "site": "제로월드",
                    "branch": context.branch,
                    "date": context.reservation_date,
                    "time": context.target_time,
                    "booking_number": booking_number,
                })
            except Exception as exc:
                self.log(f"예약은 확인됐지만 로컬 이력 저장 실패 · {self._format_exception(exc, context)}", "warning")
            try:
                webbrowser.open(
                    f"{self.home_url}?go=rev.make.end&code={urllib.parse.quote(reservation_code)}"
                )
            except Exception:
                self.log("예약은 확인됐지만 완료 화면을 열지 못했습니다.", "warning")
            return BookingResult(
                True,
                f"예약 최종 완료 · 예약번호 {booking_number or '확인 필요'}",
                booking_number=booking_number,
                details={"reservation_code": reservation_code},
            )

        debug_path = self._save_debug("zeroworld_payment_debug.html", payment_body, "결제/접수 완료")
        self.log(
            f"[{worker_name}] 예약 완료 표식 미확인 · 성공 판정 보류 · "
            f"민감정보 제외 진단 요약 저장{f' ({debug_path.name})' if debug_path else ''}",
            "warning",
        )
        return receipt

    async def _confirm_booking(self, session, context: ZeroWorldContext, number: str) -> BookingResult:
        """The live site's reservation lookup, not make/cancel/payment."""
        if not re.fullmatch(r"[0-9]{4,10}", number):
            return self._uncertain_result("예약 확인번호 형식 미확인")
        try:
            async with session.post(self.action_url,
                data={"act": "rev_view", "not_html": "Y", "name": context.name,
                      "mobile": context.phone, "ck_code": number},
                    timeout=aiohttp.ClientTimeout(total=self.SUBMIT_RECONCILE_SECONDS)) as response:
                body = decode_body(await response.read())
                if response.status != 200:
                    return self._uncertain_result("예약내역 조회 응답 오류")
        except Exception:
            # A read failure after an accepted mutation must never escape to
            # the scan loop and authorize another reservation submission.
            return self._uncertain_result("예약내역 재확인 통신 실패")
        return self._receipt_result(body, context, lookup_number=number)

    def _receipt_result(self, body: str, context: ZeroWorldContext, *, lookup_number: str = "") -> BookingResult:
        soup = BeautifulSoup(body, "html.parser")
        for node in soup.select("script, style, noscript"):
            node.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        text = re.sub(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일",
                      lambda m: f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}", text)
        number = re.search(r"예약\s*번호\s*[:：#]?\s*([A-Za-z0-9-]{3,40})(?![A-Za-z0-9-])", text)
        booking_number = number.group(1) if number else lookup_number
        if lookup_number and number and booking_number != lookup_number:
            return self._uncertain_result("조회한 예약번호와 응답의 예약번호 불일치")
        rejected = re.search(r"(?:예약\s*상태|처리\s*상태|진행\s*상태)\s*[:：]?\s*(?:취소|환불|실패|거절)", text)
        normal = re.search(r"(?:예약\s*상태|처리\s*상태|진행\s*상태)\s*[:：]?\s*(?:신청|접수|입금\s*대기|예약\s*완료|확정)(?=\s|$)", text)
        normal = normal or re.search(r"예약(?:이|\s*신청이)?\s*(?:정상적으로\s*)?(?:완료|접수)되었습니다", text)
        date_present = bool(re.search(r"(?<!\d)" + re.escape(context.reservation_date).replace(r"\-", r"[-./]") + r"(?!\d)", text))
        theme_field = soup.select_one('[name="theme_num"]')
        theme_matches = bool((theme_field and str(theme_field.get("value", "")) == context.theme)
                             or (context.theme_name and re.search(r"(?<!\w)" + re.escape(context.theme_name) + r"(?!\w)", text)))
        people = re.search(r"인원\s*[:：]?\s*(\d+)\s*명", text)
        people_match = (people.group(1) == context.people) if people else not lookup_number
        time_matches = bool(re.search(r"(?<!\d)" + re.escape(context.target_time) + r"(?!\d)", text))
        if not (booking_number and normal and not rejected and date_present and time_matches and theme_matches and people_match):
            return self._uncertain_result("예약번호·정상 접수 상태·목표 일시/테마 검증 미완료")
        return BookingResult(True, f"예약 최종 완료 · 예약번호 {booking_number}", booking_number)

    @staticmethod
    def _extract_value(body: str, name: str) -> str:
        field = BeautifulSoup(body, "html.parser").find("input", attrs={"name": name})
        return str(field.get("value", "")) if field else ""

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
