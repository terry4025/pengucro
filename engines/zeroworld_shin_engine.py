from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

from engines.base_engine import BaseEngine
from engines.zeroworld_catalog import (
    calendar_contains_date,
    decode_body,
    parse_time_slots,
    subject_for_branch,
)
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

    async def make_reservation_async_task(self, reservation_data: dict[str, Any], task_idx: int) -> None:
        try:
            context = self._build_context(reservation_data)
        except ValueError as exc:
            self.log(str(exc), "error")
            self.stop_event.set()
            return

        worker_name = f"작업 {task_idx + 1}"
        self.log(
            f"[{worker_name}] 신 제로월드 감시 시작 · 지점 {context.branch} · {context.target_time}",
            "info",
        )
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(headers=self._headers(context), timeout=timeout) as session:
            date_open = False
            while not self.stop_event.is_set():
                try:
                    if not date_open:
                        date_open = await self._wait_for_date(session, context)
                        if not date_open:
                            await asyncio.sleep(0.15)
                            continue

                    slot_id = await self._find_slot(session, context)
                    if not slot_id:
                        self.silent_tick(f"{context.target_time} 슬롯 대기")
                        self._log_throttled(
                            f"slot:{context.target_time}",
                            f"{context.target_time} 예약 오픈을 기다리는 중입니다.",
                        )
                        await asyncio.sleep(0.15)
                        continue

                    async with self.async_submission_lock:
                        if self.stop_event.is_set():
                            return
                        result = await self._submit(session, context, slot_id)
                    if result:
                        self.log(f"🎉 {result.message}", "success")
                        self.notify_success(result)
                        return
                    await asyncio.sleep(0.4)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    self._log_throttled("network", f"제로월드 통신 오류: {exc}")
                    await asyncio.sleep(0.5)
                except Exception as exc:
                    self._log_throttled("unexpected", f"제로월드 처리 오류: {exc}", "error")
                    await asyncio.sleep(0.5)

    async def _wait_for_date(self, session: aiohttp.ClientSession, context: ZeroWorldContext) -> bool:
        year, month, _ = context.reservation_date.split("-")
        payload = {
            "act": "calendar",
            "zizum_num": context.branch,
            "rev_days": context.reservation_date,
            "year": year,
            "month": month,
            "s_subj": context.subject,
        }
        async with session.post(self.select_url, data=payload) as response:
            body = decode_body(await response.read())
        if calendar_contains_date(body, context.reservation_date):
            self.log(f"📅 {context.reservation_date} 예약일 오픈을 확인했습니다.", "info")
            return True
        self.silent_tick(f"{context.reservation_date} 날짜 대기")
        self._log_throttled(
            f"date:{context.reservation_date}",
            f"{context.reservation_date} 예약일 오픈을 기다리는 중입니다.",
            "info",
        )
        return False

    async def _find_slot(self, session: aiohttp.ClientSession, context: ZeroWorldContext) -> str:
        payload = {
            "act": "theme_time_list",
            "zizum_num": context.branch,
            "rev_days": context.reservation_date,
            "theme_num": context.theme,
        }
        async with session.post(self.select_url, data=payload) as response:
            if response.status != 200:
                return ""
            body = decode_body(await response.read())
        for slot in parse_time_slots(body):
            if slot.time == context.target_time and slot.available:
                self.log(f"⏰ {context.target_time} 슬롯 발견 · ID {slot.slot_id}", "info")
                return slot.slot_id
        return ""

    async def _submit(
        self,
        session: aiohttp.ClientSession,
        context: ZeroWorldContext,
        slot_id: str,
    ) -> BookingResult | None:
        await self._post_and_discard(
            session,
            {
                "act": "theme_list",
                "zizum_num": context.branch,
                "rev_days": context.reservation_date,
                "theme_num": "",
                "s_subj": context.subject,
            },
        )
        await self._post_and_discard(
            session,
            {
                "act": "theme_select",
                "theme_num": context.theme,
                "rev_days": context.reservation_date,
                "theme_time_num": "",
            },
        )
        await self._post_and_discard(
            session,
            {"act": "theme_time_select", "theme_time_num": slot_id},
        )
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
        async with session.post(self.action_url, data=action_data) as response:
            body = decode_body(await response.read())
            final_url = str(response.url)
            history_urls = [str(item.url) for item in response.history]

        if not self._submission_accepted(body, final_url, history_urls):
            error = self._extract_alert(body) or "예약 제출이 아직 승인되지 않았습니다."
            self._log_throttled(f"submit:{error}", f"제출 대기 중: {error}")
            return None

        return await self._complete_payment(session, body, final_url, history_urls, context)

    async def _post_and_discard(self, session: aiohttp.ClientSession, data: dict[str, str]) -> None:
        async with session.post(self.select_url, data=data) as response:
            await response.read()

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
    ) -> BookingResult:
        combined = " ".join([body, final_url, *history_urls])
        reservation_code = self._extract_value(body, "code")
        check_code = self._extract_value(body, "ck_code")
        if not reservation_code:
            match = re.search(r"[?&]code=([A-Za-z0-9_-]+)", combined)
            reservation_code = match.group(1) if match else ""
        if not reservation_code:
            self._save_debug("zeroworld_submit_debug.html", body)
            return BookingResult(True, "예약 선점 성공 · 사이트에서 예약 내역을 확인해주세요.")

        if not check_code:
            kcp_url = f"{self.home_url}?go=rev.kcp&code={urllib.parse.quote(reservation_code)}"
            async with session.get(kcp_url) as response:
                check_code = self._extract_value(decode_body(await response.read()), "ck_code")

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
        async with session.post(self.payment_url, data=payment_data) as response:
            payment_body = decode_body(await response.read())

        refresh = re.search(r"url=([^'\">]+)", payment_body, re.I)
        if refresh:
            next_url = urllib.parse.urljoin(self.payment_url, refresh.group(1).strip())
            async with session.get(next_url) as response:
                payment_body += decode_body(await response.read())

        number_match = re.search(r"ck_code=(\d+)", payment_body)
        if not number_match:
            number_match = re.search(r"예약번호[^0-9]*(\d+)", payment_body)
        booking_number = number_match.group(1) if number_match else check_code
        completed = any(
            marker in payment_body
            for marker in ("rev.make.exe.php", "rev.make.end", "완료", "접수", "성공")
        )
        if completed:
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

        self._save_debug("zeroworld_payment_debug.html", payment_body)
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

    @staticmethod
    def _save_debug(filename: str, body: str) -> None:
        try:
            data_path(filename).write_text(body, encoding="utf-8")
        except OSError:
            pass
