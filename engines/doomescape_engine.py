import asyncio
import aiohttp
import requests
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from engines.base_engine import BaseEngine
from pengucro.diagnostics import format_exception
from pengucro.storage import append_history, load_json, save_json


class DoomScanGovernor:
    """Space aggregate timetable requests independently of worker count."""

    def __init__(self, idle_rate, active_rate, min_rate, active_floor_rate):
        self.idle_rate = float(idle_rate)
        self.active_rate = float(active_rate)
        self.min_rate = float(min_rate)
        self.active_floor_rate = float(active_floor_rate)
        self.phase = "idle"
        self._penalty = 0.0
        self._next_dispatch = 0.0
        self._lock = None

    @property
    def target_rate(self):
        base = self.active_rate if self.phase == "active" else self.idle_rate
        floor = self.active_floor_rate if self.phase == "active" else self.min_rate
        return max(floor, base * (0.5 ** self._penalty))

    def set_phase(self, phase):
        if phase not in ("idle", "active"):
            raise ValueError(phase)
        changed = phase != self.phase
        self.phase = phase
        return changed

    def observe_success(self, elapsed_seconds):
        elapsed = max(0.0, float(elapsed_seconds))
        if elapsed >= 1.5:
            self._penalty = min(3.0, self._penalty + 0.25)
        elif elapsed <= 0.8:
            self._penalty = max(0.0, self._penalty - 0.5)

    def observe_failure(self):
        self._penalty = min(3.0, self._penalty + 0.5)

    async def wait_turn(self, stop_event=None):
        if self._lock is None:
            self._lock = asyncio.Lock()
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            async with self._lock:
                now = time.monotonic()
                remaining = self._next_dispatch - now
                if remaining <= 0:
                    self._next_dispatch = now + (1.0 / self.target_rate)
                    return
            # Do not reserve dispatches far into the future. This keeps a queue
            # created during the idle phase from delaying the open-time burst.
            await asyncio.sleep(min(0.05, remaining))

class DoomEscapeEngine(BaseEngine):
    LIST_TIMEOUT_SECONDS = 1.25
    REQUEST_TIMEOUT_SECONDS = 5
    IDLE_SCAN_RATE_PER_SECOND = 2.0
    ACTIVE_SCAN_RATE_PER_SECOND = 12.0
    MIN_SCAN_RATE_PER_SECOND = 1.0
    ACTIVE_SCAN_FLOOR_PER_SECOND = 8.0
    MAX_SCAN_INFLIGHT = 16
    MAX_WARM_INFLIGHT = 4
    MAX_SCAN_SESSIONS = 8
    OPEN_ANCHOR_LEAD_SECONDS = 3.0
    OPEN_ANCHOR_HOLD_SECONDS = 1800.0
    DEFAULT_OPEN_TIME = "23:45:00"
    OPEN_TIME_MEMORY_FILE = "doomescape_open_times.json"
    REQUEST_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    THEME_ID_TO_NAME = {
        # 1호점
        "8": "Rendering",
        "27": "기담정",
        "28": "인앤아웃",
        "29": "나폴리탄",
        # 2호점
        "30": "운명",
        "31": "디스토피아",
        "32": "죄",
        "33": "인바이트",
        # DTH점(부평)
        "19": "슬래셔",
        "22": "트리거",
        "24": "언리얼",
        "25": "스네어",
        # FEAR점(수원)
        "34": "허수아비",
        "35": "옵스큐라",
        "36": "데이투어"
    }

    def __init__(self, site_url, log_callback, success_callback=None):
        """
        Doom Escape (Sinbiweb-based) Booking Engine.
        """
        super().__init__(log_callback, success_callback)
        self.site_url = site_url or "https://doomescape.com"
        parsed = urllib.parse.urlparse(self.site_url)
        self.base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else self.site_url.rstrip("/")
        
        import threading
        self._log_lock = threading.Lock()
        self._notified_bypass = False
        self._notified_found = False
        self._last_err_msg = ""
        self._last_err_time = 0
        self._last_wait_time = 0
        self._outage_started_at = 0.0
        self._diagnostic_log_state = {}
        self._sync_worker_index = 0
        self._slot_wait_started_at = None
        self.scan_governor = None
        self._scan_inflight = None
        self._scan_session_count = 0
        self._submit_session = None
        self._open_anchor_epoch = None
        self._last_slot_reason = None
        self._open_time_recorded = False
        self._branch_id = "3"
        self._outage_reported = False
        self._outage_started_at = 0.0
        self._last_scan_failure_at = 0.0
        self._prestaged_prices = None
        self._prestage_lock = None
        self._inventory_logged = False
        self._unverified_date_warned = False

    @staticmethod
    def _describe_exception(exc):
        message = format_exception(exc)
        # Doom's completion request carries ck_code in the query string.  Some
        # HTTP client exceptions include the URL, so remove that one-time value
        # before either the UI or persistent logger can see it.
        return re.sub(
            r"(?i)(?P<prefix>[?&]ck_code=)[^&#\s]+",
            r"\g<prefix>[redacted]",
            message,
        )

    def _next_sync_worker_label(self):
        with self._log_lock:
            self._sync_worker_index += 1
            return f"동기 작업 {self._sync_worker_index}"

    def _log_http_diagnostic(
        self,
        worker,
        stage,
        method,
        status,
        elapsed_seconds,
        *,
        detail="",
        force=False,
    ):
        """Emit bounded, non-sensitive request diagnostics.

        Slot polling can run many times per second, so successful repeats are
        summarized at most once every five seconds.  No URL, request body,
        response body, cookie, token, name, phone number, or payment value is
        included.
        """
        status_text = str(status) if status is not None else "연결 실패"
        detail_text = str(detail).strip()
        aggregate_scan = str(stage) == "시간표 조회"
        display_worker = "전체 감시" if aggregate_scan else str(worker)
        key = (display_worker, str(stage), str(method), status_text, detail_text)
        now = time.monotonic()
        with self._log_lock:
            state = self._diagnostic_log_state.setdefault(
                key, {"last": 0.0, "count": 0}
            )
            state["count"] += 1
            should_log = force or not state["last"] or now - state["last"] >= 5.0
            if not should_log:
                return
            attempts = state["count"]
            state["last"] = now
            state["count"] = 0

        repeat = f" · 최근 {attempts}회" if attempts > 1 else ""
        suffix = f" · {detail_text}" if detail_text else ""
        level = "info" if str(status_text).startswith("2") else "warning"
        self.log(
            f"[{display_worker}] [HTTP] {stage} · {method} · status={status_text} · "
            f"RTT {max(0.0, elapsed_seconds) * 1000:.0f}ms{repeat}{suffix}",
            level,
        )

    @staticmethod
    def _safe_response_markers(text):
        lowered = (text or "").casefold()
        return {
            "has_completion_marker": any(
                marker in lowered
                for marker in ("rev.make.exe.php", "rev.make.end", "완료", "성공")
            ),
            "has_alert": "alert(" in lowered or "alert (" in lowered,
            "has_meta_refresh": "http-equiv=\"refresh\"" in lowered
            or "http-equiv='refresh'" in lowered,
        }

    def _write_safe_failure_summary(
        self,
        *,
        worker,
        stage,
        status,
        response_text,
        slot_id="",
        order_id="",
    ):
        """Persist metadata only; raw reservation HTML is never written."""
        try:
            diagnostic_dir = Path("scratch")
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "engine": "doomescape",
                "worker": str(worker),
                "stage": str(stage),
                "http_status": status,
                "response_bytes": len((response_text or "").encode("utf-8", errors="ignore")),
                "slot_id": str(slot_id),
                "order_id": str(order_id),
                "markers": self._safe_response_markers(response_text),
            }
            target = diagnostic_dir / "last_mutong_diagnostic.json"
            temporary = diagnostic_dir / "last_mutong_diagnostic.json.tmp"
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, target)
            self.log(
                f"[{worker}] [진단] {stage} 응답은 원문 대신 민감정보를 제외한 요약으로 저장했습니다.",
                "warning",
            )
        except Exception as exc:
            self.log(
                f"[{worker}] [진단] 안전 진단 요약 저장 실패 · {self._describe_exception(exc)}",
                "warning",
            )

    @staticmethod
    def _reservation_page_looks_healthy(html_text):
        lowered = (html_text or "").lower()
        return bool(
            "tm_box" in lowered
            or "rev_days" in lowered
            or "go=rev.make" in lowered
        )

    SLOT_STATE_AVAILABLE = "예약가능"
    SLOT_STATE_CLOSED = "예약마감"

    @classmethod
    def analyze_timetable(cls, html_text, theme_name, rev_days, target_time):
        """Return a target-date-verified inventory for one theme."""

        soup = BeautifulSoup(html_text or "", "html.parser")
        boxes = soup.find_all("div", class_="tm_box")
        page_dates = set()
        # Date-navigation links can already contain the future target date while
        # the timetable below is still showing today. Trust only links inside
        # rendered timetable boxes, then fall back to the page's hidden state.
        for box in boxes:
            for anchor in box.find_all("a", href=True):
                match = re.search(r"rev_days=(\d{4}-\d{2}-\d{2})", anchor["href"])
                if match:
                    page_dates.add(match.group(1))
        if not page_dates:
            for field in soup.find_all("input"):
                if (field.get("name") or "").strip() != "rev_days":
                    continue
                value = (field.get("value") or "").strip()
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    page_dates.add(value)

        date_verified = bool(page_dates)
        date_matches = not rev_days or not date_verified or rev_days in page_dates
        result = {
            "page_dates": sorted(page_dates),
            "date_verified": date_verified,
            "date_matches": date_matches,
            "theme_found": False,
            "times": [],
            "slot_ids": {},
            "slot_id": None,
            "reason": "미오픈",
        }
        if not boxes:
            return result

        target_box = None
        for box in boxes:
            name_p = box.find("p", class_="name")
            if name_p and theme_name and theme_name in name_p.text:
                target_box = box
                break
        result["theme_found"] = target_box is not None
        if target_box is None:
            result["reason"] = "오픈됨·테마 미표시" if date_matches else "날짜 미공개"
            return result

        target_state = None
        target_normalized = cls._normalize_time_text(target_time)
        for num_span in target_box.find_all("span", class_="num"):
            time_text = num_span.get_text(strip=True)
            if not time_text:
                continue
            anchor = num_span.find_parent("a", href=True)
            container = anchor if anchor is not None else num_span.parent
            txt_span = container.find("span", class_="txt") if container else None
            state_text = txt_span.get_text(strip=True) if txt_span else ""
            href = anchor["href"] if anchor is not None else ""
            slot_match = re.search(r"theme_time_num=(\d+)", href)
            closed = cls.SLOT_STATE_CLOSED in state_text or not slot_match
            state = cls.SLOT_STATE_CLOSED if closed else cls.SLOT_STATE_AVAILABLE
            if not any(existing[0] == time_text for existing in result["times"]):
                result["times"].append((time_text, state))
            if slot_match and not closed:
                result["slot_ids"].setdefault(time_text, slot_match.group(1))
            if target_normalized and target_normalized == cls._normalize_time_text(time_text):
                target_state = state
                if not closed and date_matches:
                    result["slot_id"] = slot_match.group(1)

        if not date_matches:
            result["slot_id"] = None
            result["reason"] = "날짜 미공개"
        elif result["slot_id"]:
            result["reason"] = "예약가능"
        elif target_state == cls.SLOT_STATE_CLOSED:
            result["reason"] = "예약마감"
        else:
            result["reason"] = "해당 시각 없음"
        return result

    @classmethod
    def _classify_missing_slot(cls, html_text, theme_name, rev_days=None, target_time=None):
        analysis = cls.analyze_timetable(html_text, theme_name, rev_days, target_time)
        reason = analysis["reason"]
        if reason == "해당 시각 없음" and not target_time:
            return "오픈됨·해당 시간 없음"
        return reason

    @staticmethod
    def _normalize_time_text(value):
        match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", str(value or ""))
        if not match:
            return ""
        return f"{int(match.group(1)):02d}:{match.group(2)}"

    @staticmethod
    def _extract_price_fields(input_html):
        fields = {}
        soup = BeautifulSoup(input_html or "", "html.parser")
        for hidden in soup.find_all("input"):
            if str(hidden.get("type") or "").casefold() != "hidden":
                continue
            name = str(hidden.get("name") or "")
            value = str(hidden.get("value") or "")
            if re.fullmatch(r"price\d*", name, re.I) and value.isdigit():
                fields[name] = value
        return fields

    @staticmethod
    def _prestage_rejection_requires_refresh(status, response_text):
        if int(status or 0) >= 500:
            return False
        alert = re.search(
            r"alert\s*\(\s*['\"](.*?)['\"]\s*\)", response_text or "", re.I | re.S
        )
        message = alert.group(1) if alert else ""
        capacity_markers = ("마감", "예약 불가", "예약불가", "이미 예약", "선택하신 시간")
        if any(marker in message for marker in capacity_markers):
            return False
        form_markers = ("가격", "금액", "인원", "price", "파라미터", "필수", "잘못된")
        return any(marker.casefold() in message.casefold() for marker in form_markers)

    @classmethod
    def _open_anchor_from_wall_clock(cls, value, now=None):
        reference = now or datetime.now()
        match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", str(value or "").strip())
        if not match:
            return None
        hour, minute, second = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
        if hour > 23 or minute > 59 or second > 59:
            return None
        candidate = reference.replace(hour=hour, minute=minute, second=second, microsecond=0)
        previous = candidate - timedelta(days=1)
        if previous >= reference - timedelta(seconds=cls.OPEN_ANCHOR_HOLD_SECONDS):
            candidate = previous
        elif candidate < reference - timedelta(seconds=cls.OPEN_ANCHOR_HOLD_SECONDS):
            candidate += timedelta(days=1)
        return candidate.timestamp()

    @classmethod
    def load_learned_open_time(cls, branch):
        data = load_json(cls.OPEN_TIME_MEMORY_FILE, {})
        entry = (data.get("branches") or {}).get(str(branch)) if isinstance(data, dict) else None
        return str(entry.get("open_time") or "") if isinstance(entry, dict) else ""

    def _record_open_time(self):
        if self._open_time_recorded:
            return
        self._open_time_recorded = True
        observed = datetime.now().replace(second=0, microsecond=0)
        data = load_json(self.OPEN_TIME_MEMORY_FILE, {})
        if not isinstance(data, dict):
            data = {}
        branches = data.setdefault("branches", {})
        branches[str(self._branch_id)] = {
            "open_time": observed.strftime("%H:%M:%S"),
            "observed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            save_json(self.OPEN_TIME_MEMORY_FILE, data)
        except OSError:
            return

    def _update_scan_phase(self, reason):
        governor = self.scan_governor
        if governor is None:
            return
        now = time.time()
        active_by_clock = bool(
            self._open_anchor_epoch is not None
            and self._open_anchor_epoch - self.OPEN_ANCHOR_LEAD_SECONDS
            <= now
            <= self._open_anchor_epoch + self.OPEN_ANCHOR_HOLD_SECONDS
        )
        published = reason not in ("미오픈", "날짜 미공개")
        phase = "active" if active_by_clock or published else "idle"
        if (
            self._last_slot_reason in ("미오픈", "날짜 미공개")
            and published
            and not self._outage_started_at
        ):
            self._record_open_time()
        self._last_slot_reason = reason
        if governor.set_phase(phase):
            self.log(
                f"[정보] 감시 속도 전환 · {'집중' if phase == 'active' else '대기'} "
                f"{governor.target_rate:.0f}회/초 · 동시 응답 대기 최대 {self.MAX_SCAN_INFLIGHT}개",
                "info",
            )

    def _log_inventory_once(self, worker_label, rev_days, target_time, analysis):
        if not analysis["date_verified"] and not self._unverified_date_warned:
            self._unverified_date_warned = True
            self.log(
                f"[{worker_label}] [주의] 시간표 응답의 날짜를 검증할 수 없습니다.",
                "warning",
            )
        if self._inventory_logged or not analysis["theme_found"]:
            return
        if not analysis["date_matches"] or not analysis["times"]:
            return
        self._inventory_logged = True
        inventory = " · ".join(f"{stamp}({state})" for stamp, state in analysis["times"])
        self.log(
            f"[{worker_label}] [진단] {rev_days} 회차 목록 · {inventory} · 목표 {target_time} "
            f"{'확인' if any(self._normalize_time_text(target_time) == self._normalize_time_text(stamp) for stamp, _ in analysis['times']) else '없음'}",
            "info",
        )

    def _log_missing_slot(self, worker_label, target_time, reason):
        now = time.time()
        show_log = False
        with self._log_lock:
            if not hasattr(self, "_last_time_err_time") or (
                now - self._last_time_err_time
            ) > 10.0:
                self._last_time_err_time = now
                show_log = True
        if show_log:
            elapsed_text = ""
            started_at = getattr(self, "_slot_wait_started_at", None)
            if started_at:
                elapsed_text = f" · 대기 {now - started_at:.0f}초"
            rate_text = ""
            if self.scan_governor is not None:
                rate_text = f" · 조회 {self.scan_governor.target_rate:.1f}회/초"
            self.log(
                f"[{worker_label}] [대기] {target_time} 슬롯 {reason}"
                f"{elapsed_text}{rate_text} · 오픈 시까지 계속 조회합니다",
                "info",
            )

    def _note_scan_failure(self, worker_label, stage, exc):
        if self.scan_governor is not None:
            self.scan_governor.observe_failure()
        now = time.monotonic()
        with self._log_lock:
            self._last_scan_failure_at = now
            if not self._outage_started_at:
                self._outage_started_at = now
            should_report = not self._outage_reported
            if should_report:
                self._outage_reported = True
        if should_report:
            self.log(
                f"[{worker_label}] 서버 응답 불안정 · 단계={stage} · "
                f"{self._describe_exception(exc)} · 전체 정지 없이 독립 감시를 계속합니다.",
                "warning",
            )

    def _note_scan_success(self, elapsed_seconds):
        if self.scan_governor is not None:
            self.scan_governor.observe_success(elapsed_seconds)
        now = time.monotonic()
        with self._log_lock:
            started = self._outage_started_at
            reported = self._outage_reported
            stable = bool(started and now - self._last_scan_failure_at >= 0.5)
            if stable:
                self._outage_started_at = 0.0
                self._outage_reported = False
        if reported and stable:
            self.log(
                f"[정보] 둠이스케이프 서버 첫 정상 응답 확보 · "
                f"{now - started:.1f}초 · 즉시 선점 경로를 재개합니다.",
                "success",
            )

    async def _prestage_prices(self, analysis, theme_name):
        if self._prestaged_prices is not None or self._submit_session is None:
            return
        if self._prestage_lock is None:
            self._prestage_lock = asyncio.Lock()
        if self._prestage_lock.locked():
            return
        candidate_time = next(iter(analysis["slot_ids"]), "")
        source_date = analysis["page_dates"][0] if analysis["page_dates"] else ""
        slot_id = analysis["slot_ids"].get(candidate_time, "")
        if not slot_id or not source_date:
            return
        async with self._prestage_lock:
            if self._prestaged_prices is not None:
                return
            url = (
                f"{self.base_url}/layout/res/home.php?go=rev.make.input"
                f"&rev_days={source_date}&theme_time_num={slot_id}"
            )
            try:
                started = time.perf_counter()
                async with self._submit_session.get(url, timeout=3.0) as response:
                    body = await response.read()
                    status = response.status
                fields = self._extract_price_fields(body.decode("utf-8", errors="ignore"))
                self._log_http_diagnostic(
                    "제출 전용", "예약 입력값 사전 준비", "GET", status,
                    time.perf_counter() - started, force=True,
                )
                if fields:
                    self._prestaged_prices = fields
                    self.log(
                        f"[사전준비] {theme_name} 제출 값 확보 완료 · "
                        "회차 공개 시 주문 생성 요청부터 즉시 전송합니다.",
                        "success",
                    )
            except Exception as exc:
                self.log(
                    f"[사전준비] 제출 값 확보 지연 · {self._describe_exception(exc)} · "
                    "회차 공개 후 안전 경로를 사용합니다.",
                    "warning",
                )

    async def _read_price_fields_async(self, session, input_url, worker_label, slot_id):
        started = time.perf_counter()
        async with session.get(input_url, timeout=5) as response:
            status = response.status
            body = await response.read()
        self._log_http_diagnostic(
            worker_label, "예약 입력 화면 조회", "GET", status,
            time.perf_counter() - started, detail=f"slotId={slot_id}", force=True,
        )
        return self._extract_price_fields(body.decode("utf-8", errors="ignore"))

    @staticmethod
    def _is_transient_site_error(exc):
        if isinstance(exc, (asyncio.TimeoutError, aiohttp.ClientError, ConnectionError)):
            return True
        message = str(exc)
        return message.startswith("HTTP ") or message.startswith("INVALID_RESERVATION_PAGE")

    def make_reservation_thread(self, reservation_data):
        worker_label = self._next_sync_worker_label()
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        session.headers.update(headers)

        zizum_num = reservation_data.get("branch", "3")
        rev_days = reservation_data.get("reservationDate")
        theme_num = reservation_data.get("themePK")
        target_time = reservation_data.get("reservationTime")[:5]
        name = reservation_data.get("name")
        phone = reservation_data.get("phone", "")
        phone_digits = "".join(c for c in phone if c.isdigit())
        people = reservation_data.get("people", "3")

        # Split phone number into 3 parts
        if len(phone_digits) == 11:
            mobile1 = phone_digits[0:3]
            mobile2 = phone_digits[3:7]
            mobile3 = phone_digits[7:11]
        elif len(phone_digits) == 10:
            mobile1 = phone_digits[0:3]
            mobile2 = phone_digits[3:6]
            mobile3 = phone_digits[6:10]
        else:
            mobile1 = "010"
            mobile2 = "1234"
            mobile3 = "5678"

        theme_name = reservation_data.get("themeLabel") or self.THEME_ID_TO_NAME.get(theme_num, "")

        list_url = f"{self.base_url}/layout/res/home.php?go=rev.make&s_zizum={zizum_num}&rev_days={rev_days}"
        act_url = f"{self.base_url}/core/res/rev.act.php"

        self.log(f"[{worker_label}] 둠이스케이프 동기 작업 시작 (지점: {zizum_num}, 테마: {theme_name}({theme_num}), 날짜: {rev_days}, 시간: {target_time})", "info")

        while not self.stop_event.is_set():
            current_stage = "시간표 조회"
            try:
                # 1. Fetch reservation page to list slots
                request_started = time.perf_counter()
                resp = session.get(list_url, timeout=5)
                request_rtt = time.perf_counter() - request_started
                self._log_http_diagnostic(
                    worker_label,
                    current_stage,
                    "GET",
                    resp.status_code,
                    request_rtt,
                    detail="100ms 후 재시도" if resp.status_code != 200 else "",
                    force=resp.status_code != 200,
                )
                if resp.status_code != 200:
                    self.silent_tick(
                        f"[{worker_label}] 시간표 조회 실패 · HTTP {resp.status_code} · 100ms 후 재시도"
                    )
                    time.sleep(0.1)
                    continue

                html_text = resp.content.decode('utf-8', errors='ignore')
                
                # Parse available slots
                soup = BeautifulSoup(html_text, 'html.parser')
                found_slot = None
                
                # Find matching theme box
                target_box = None
                for box in soup.find_all('div', class_='tm_box'):
                    name_p = box.find('p', class_='name')
                    if name_p and theme_name in name_p.text:
                        target_box = box
                        break
                
                if not target_box:
                    self.silent_tick("테마 박스를 찾을 수 없음")
                    time.sleep(0.1)
                    continue

                # Look for target time in theme box
                for a in target_box.find_all('a'):
                    num_span = a.find('span', class_='num')
                    txt_span = a.find('span', class_='txt')
                    if num_span and target_time in num_span.text:
                        if txt_span and "예약마감" in txt_span.text:
                            continue
                        
                        href = a.get('href', '')
                        match = re.search(r"theme_time_num=(\d+)", href)
                        if match:
                            found_slot = match.group(1)
                            break

                if not found_slot:
                    reason = self._classify_missing_slot(html_text, theme_name)
                    self._log_missing_slot(worker_label, target_time, reason)
                    time.sleep(0.1)
                    continue

                slot_id = found_slot
                show_found = False
                with self._log_lock:
                    if not self._notified_found:
                        self._notified_found = True
                        show_found = True
                if show_found:
                    self.log(
                        f"[{worker_label}] [슬롯 확인] 시간 {target_time} · slotId={slot_id} · 예약 제출 단계로 이동",
                        "info",
                    )

                if self.stop_event.is_set():
                    break

                lock_acquired = False
                if hasattr(self, "submission_lock"):
                    lock_acquired = self.submission_lock.acquire(block=False)
                
                if hasattr(self, "submission_lock") and not lock_acquired:
                    time.sleep(0.1)
                    continue

                try:
                    if self.stop_event.is_set():
                        break

                    # 2. Visit input page to extract prices dynamically
                    current_stage = "예약 입력 화면 조회"
                    input_url = f"{self.base_url}/layout/res/home.php?go=rev.make.input&rev_days={rev_days}&theme_time_num={slot_id}"
                    request_started = time.perf_counter()
                    resp_input = session.get(input_url, timeout=5)
                    self._log_http_diagnostic(
                        worker_label,
                        current_stage,
                        "GET",
                        resp_input.status_code,
                        time.perf_counter() - request_started,
                        detail=f"slotId={slot_id}",
                        force=True,
                    )
                    input_html = resp_input.content.decode('utf-8', errors='ignore')

                    price_fields = {}
                    # Extract hidden price fields
                    for p_inp in re.findall(r'<input[^>]*type=["\']?hidden["\']?[^>]*>', input_html, re.I):
                        name_m = re.search(r'name=["\']?(price\d*)["\']?', p_inp, re.I)
                        val_m = re.search(r'value=["\']?(\d+)["\']?', p_inp, re.I)
                        if name_m and val_m:
                            price_fields[name_m.group(1)] = val_m.group(1)

                    # Default fallback prices if scraping fails
                    base_price = price_fields.get("price", "126000")
                    price1 = price_fields.get("price1", base_price)
                    price2 = price_fields.get("price2", base_price)
                    price3 = price_fields.get("price3", base_price)
                    price4 = price_fields.get("price4", "168000")
                    price5 = price_fields.get("price5", "210000")
                    price6 = price_fields.get("price6", "252000")

                    # Use selected person price
                    actual_price = price_fields.get(f"price{people}", base_price)

                    # 3. Post to rev.act.php (UTF-8 encoding required!)
                    act_data = {
                        "name": name,
                        "mobile1": mobile1,
                        "mobile2": mobile2,
                        "mobile3": mobile3,
                        "person": people,
                        "ck_agree": "on",
                        "rev_days": rev_days,
                        "theme_time_num": slot_id,
                        "price": actual_price,
                        "price1": price1,
                        "price2": price2,
                        "price3": price3,
                        "price4": price4,
                        "price5": price5,
                        "price6": price6,
                        "act": "make",
                        "layout_folder": "layout/res"
                    }

                    encoded_pairs = []
                    for k, v in act_data.items():
                        encoded_pairs.append((k.encode('utf-8'), v.encode('utf-8')))
                    post_data = urllib.parse.urlencode(encoded_pairs).encode()

                    post_headers = {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": input_url,
                        "Origin": self.base_url
                    }

                    current_stage = "예약 주문 생성"
                    request_started = time.perf_counter()
                    act_resp = session.post(act_url, data=post_data, headers=post_headers, timeout=8)
                    self._log_http_diagnostic(
                        worker_label,
                        current_stage,
                        "POST",
                        act_resp.status_code,
                        time.perf_counter() - request_started,
                        detail=f"slotId={slot_id}",
                        force=True,
                    )
                    act_text = act_resp.content.decode('utf-8', errors='ignore')

                    # Parse response to find num
                    num_m = re.search(r"num=(\d+)", act_text)
                    if num_m:
                        num = num_m.group(1)
                        self.log(
                            f"[{worker_label}] [주문 생성] slotId={slot_id} · orderId={num}",
                            "info",
                        )
                        kcp_url = f"{self.base_url}/layout/res/home.php?go=rev.kcp&num={num}"
                        
                        # 4. Fetch KCP page to extract ck_code
                        current_stage = "결제 준비 화면 조회"
                        request_started = time.perf_counter()
                        kcp_resp = session.get(kcp_url, timeout=8)
                        self._log_http_diagnostic(
                            worker_label,
                            current_stage,
                            "GET",
                            kcp_resp.status_code,
                            time.perf_counter() - request_started,
                            detail=f"orderId={num}",
                            force=True,
                        )
                        kcp_text = kcp_resp.content.decode('utf-8', errors='ignore')
                        
                        ck_m = re.search(r"name=['\"]?ck_code['\"]?\s*value=['\"]?([^'\"'>\s]+)", kcp_text)
                        ck_code_val = ck_m.group(1) if ck_m else ""

                        # 5. Submit mutong.php (using GET or POST - GET is browser default)
                        mutong_url = f"{self.base_url}/core/res/rev.make.mutong.php"
                        mutong_params = {
                            "num": num,
                            "ck_code": ck_code_val,
                            "layout_folder": "layout/res",
                            "payment": "D"  # 'D' is mutong for Doom Escape
                        }
                        
                        # Request using GET
                        query_str = urllib.parse.urlencode(mutong_params)
                        mutong_get_url = f"{mutong_url}?{query_str}"
                        current_stage = "무통장 예약 확정"
                        request_started = time.perf_counter()
                        mutong_resp = session.get(mutong_get_url, timeout=8)
                        mutong_status = mutong_resp.status_code
                        self._log_http_diagnostic(
                            worker_label,
                            current_stage,
                            "GET",
                            mutong_status,
                            time.perf_counter() - request_started,
                            detail=f"orderId={num}",
                            force=True,
                        )
                        mutong_text = mutong_resp.content.decode('utf-8', errors='ignore')

                        # Follow meta refresh -> rev.make.exe.php
                        refresh_m = re.search(r"url=([^'\"\>]+)", mutong_text, re.I)
                        if refresh_m:
                            next_url = refresh_m.group(1).strip()
                            if not next_url.startswith("http"):
                                next_url = urllib.parse.urljoin(mutong_url, next_url)
                            try:
                                current_stage = "예약 완료 화면 확인"
                                request_started = time.perf_counter()
                                exe_resp = session.get(next_url, timeout=8)
                                self._log_http_diagnostic(
                                    worker_label,
                                    current_stage,
                                    "GET",
                                    exe_resp.status_code,
                                    time.perf_counter() - request_started,
                                    detail=f"orderId={num}",
                                    force=True,
                                )
                                exe_text = exe_resp.content.decode('utf-8', errors='ignore')
                                mutong_text += exe_text
                            except Exception as exc:
                                self.log(
                                    f"[{worker_label}] [재시도 불가] {current_stage} · {self._describe_exception(exc)} · 이전 응답으로 결과 판정",
                                    "warning",
                                )

                        # Extract final booking number (ck_code)
                        bnum_m = re.search(r"ck_code=(\d+)", mutong_text)
                        booking_number = bnum_m.group(1) if bnum_m else ""
                        completion_code = booking_number or ck_code_val

                        if "rev.make.exe.php" in mutong_text or "rev.make.end" in mutong_text or "완료" in mutong_text or "성공" in mutong_text:
                            final_msg = (
                                f"예약 최종 완료! 예약번호: {booking_number}"
                                if booking_number
                                else "예약 최종 완료! 예약번호는 완료 화면에서 확인해주세요."
                            )
                            try:
                                import webbrowser
                                webbrowser.open(f"{self.base_url}/layout/res/home.php?go=rev.make.end&num={num}&ck_code={completion_code}")
                            except Exception:
                                pass
                            try:
                                append_history({
                                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                    "site": "둠이스케이프",
                                    "date": rev_days,
                                    "time": target_time,
                                    "booking_number": booking_number,
                                })
                            except Exception:
                                pass
                        else:
                            self._write_safe_failure_summary(
                                worker=worker_label,
                                stage="무통장 예약 결과 판정",
                                status=mutong_status,
                                response_text=mutong_text,
                                slot_id=slot_id,
                                order_id=num,
                            )
                            final_msg = (
                                f"예약 선점 성공! 예약번호: {booking_number} / 임시번호: {num} "
                                "(결제확인 응답 재확인 필요)"
                                if booking_number
                                else f"예약 선점 성공! 임시번호: {num} (결제확인 응답 재확인 필요)"
                            )
                    else:
                        err_msg = "선점 실패"
                        alert_match = re.search(r"alert\s*\(\s*['\"](.*?)['\"]\s*\)", act_text)
                        if alert_match:
                            err_msg = alert_match.group(1)
                        raise Exception(err_msg)

                    self.log(f"🎉 {final_msg}", "success")
                    self.notify_success()
                    break

                finally:
                    if hasattr(self, "submission_lock"):
                        try:
                            self.submission_lock.release()
                        except RuntimeError:
                            pass

            except Exception as e:
                err_str = self._describe_exception(e)
                now = time.time()
                show_log = False
                with self._log_lock:
                    if err_str != self._last_err_msg or (now - self._last_err_time) > 3.0:
                        self._last_err_msg = err_str
                        self._last_err_time = now
                        show_log = True
                if show_log:
                    self.log(
                        f"[{worker_label}] [오류] {current_stage} · {err_str} · 100ms 후 재시도",
                        "error",
                    )
                time.sleep(0.1)

    async def make_reservation_async_task(self, reservation_data, task_idx):
        """
        Doom Escape Asynchronous Booking Path
        """
        import aiohttp
        import asyncio
        headers = dict(self.REQUEST_HEADERS)
        
        zizum_num = reservation_data.get("branch", "3")
        rev_days = reservation_data.get("reservationDate")
        theme_num = reservation_data.get("themePK")
        target_time = reservation_data.get("reservationTime")[:5]
        name = reservation_data.get("name")
        phone = reservation_data.get("phone", "")
        phone_digits = "".join(c for c in phone if c.isdigit())
        people = reservation_data.get("people", "3")

        # Split phone number into 3 parts
        if len(phone_digits) == 11:
            mobile1 = phone_digits[0:3]
            mobile2 = phone_digits[3:7]
            mobile3 = phone_digits[7:11]
        elif len(phone_digits) == 10:
            mobile1 = phone_digits[0:3]
            mobile2 = phone_digits[3:6]
            mobile3 = phone_digits[6:10]
        else:
            mobile1 = "010"
            mobile2 = "1234"
            mobile3 = "5678"

        theme_name = reservation_data.get("themeLabel") or self.THEME_ID_TO_NAME.get(theme_num, "")
        
        list_url = f"{self.base_url}/layout/res/home.php?go=rev.make&s_zizum={zizum_num}&rev_days={rev_days}"
        act_url = f"{self.base_url}/core/res/rev.act.php"

        session = None
        if hasattr(self, "session_pool") and self._scan_session_count > 0:
            session = self.session_pool[task_idx % self._scan_session_count]
        if not session:
            session = aiohttp.ClientSession(headers=headers)

        worker_label = f"태스크 {task_idx + 1}"
        if task_idx == 0:
            self.log("[정보] 둠이스케이프 독립 감시 루프를 시작합니다.", "info")

        while not self.stop_event.is_set():
            current_stage = "시간표 조회"
            try:
                # 1. Fetch reservation page to list slots
                self._update_scan_phase(self._last_slot_reason or "미오픈")
                if self._scan_inflight is None:
                    self._scan_inflight = asyncio.Semaphore(self.MAX_SCAN_INFLIGHT)
                async with self._scan_inflight:
                    if self.scan_governor is not None:
                        await self.scan_governor.wait_turn(self.stop_event)
                    if self.stop_event.is_set():
                        break
                    request_started = time.perf_counter()
                    async with session.get(
                        list_url, timeout=self.LIST_TIMEOUT_SECONDS
                    ) as resp:
                        list_status = resp.status
                        if resp.status != 200:
                            await resp.read()
                            self._log_http_diagnostic(
                                worker_label,
                                current_stage,
                                "GET",
                                list_status,
                                time.perf_counter() - request_started,
                                detail="독립 감시 유지",
                            )
                            raise RuntimeError(f"HTTP {resp.status}")
                        html_bytes = await resp.read()
                    request_elapsed = time.perf_counter() - request_started
                html_text = html_bytes.decode('utf-8', errors='ignore')

                self._log_http_diagnostic(
                    worker_label,
                    current_stage,
                    "GET",
                    list_status,
                    request_elapsed,
                )

                if not self._reservation_page_looks_healthy(html_text):
                    raise RuntimeError("INVALID_RESERVATION_PAGE: 예약 페이지 형식 없음")
                self._note_scan_success(request_elapsed)

                analysis = self.analyze_timetable(
                    html_text, theme_name, rev_days, target_time
                )
                self._update_scan_phase(analysis["reason"])
                self._log_inventory_once(
                    worker_label, rev_days, target_time, analysis
                )
                await self._prestage_prices(analysis, theme_name)
                found_slot = analysis["slot_id"]
                if not found_slot:
                    reason = analysis["reason"]
                    self._log_missing_slot(worker_label, target_time, reason)
                    continue

                slot_id = found_slot
                show_found = False
                with self._log_lock:
                    if not self._notified_found:
                        self._notified_found = True
                        show_found = True
                if show_found:
                    self.log(
                        f"[{worker_label}] [슬롯 확인] 시간 {target_time} · slotId={slot_id} · 예약 제출 단계로 이동",
                        "info",
                    )

                if self.stop_event.is_set():
                    break

                lock_acquired = False
                if hasattr(self, "submission_lock"):
                    lock_acquired = self.submission_lock.acquire(block=False)
                
                if hasattr(self, "submission_lock") and not lock_acquired:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    if self.stop_event.is_set():
                        break

                    booking_session = self._submit_session or session
                    input_url = f"{self.base_url}/layout/res/home.php?go=rev.make.input&rev_days={rev_days}&theme_time_num={slot_id}"
                    used_prestage = bool(self._prestaged_prices)
                    price_fields = dict(self._prestaged_prices or {})
                    if not price_fields:
                        current_stage = "예약 입력 화면 조회"
                        price_fields = await self._read_price_fields_async(
                            booking_session, input_url, worker_label, slot_id
                        )

                    async def create_order(fields):
                        base_price = fields.get("price", "126000")
                        act_data = {
                            "name": name,
                            "mobile1": mobile1,
                            "mobile2": mobile2,
                            "mobile3": mobile3,
                            "person": people,
                            "ck_agree": "on",
                            "rev_days": rev_days,
                            "theme_time_num": slot_id,
                            "price": fields.get(f"price{people}", base_price),
                            "price1": fields.get("price1", base_price),
                            "price2": fields.get("price2", base_price),
                            "price3": fields.get("price3", base_price),
                            "price4": fields.get("price4", "168000"),
                            "price5": fields.get("price5", "210000"),
                            "price6": fields.get("price6", "252000"),
                            "act": "make",
                            "layout_folder": "layout/res",
                        }
                        encoded_pairs = [
                            (str(key).encode("utf-8"), str(value).encode("utf-8"))
                            for key, value in act_data.items()
                        ]
                        post_data = urllib.parse.urlencode(encoded_pairs).encode()
                        post_headers = {
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Referer": input_url,
                            "Origin": self.base_url,
                            "User-Agent": headers["User-Agent"],
                        }
                        started = time.perf_counter()
                        async with booking_session.post(
                            act_url, data=post_data, headers=post_headers, timeout=8
                        ) as response:
                            status = response.status
                            text = (await response.read()).decode("utf-8", errors="ignore")
                        self._log_http_diagnostic(
                            worker_label, "예약 주문 생성", "POST", status,
                            time.perf_counter() - started,
                            detail=f"slotId={slot_id}", force=True,
                        )
                        return status, text

                    current_stage = "예약 주문 생성"
                    act_status, act_text = await create_order(price_fields)
                    if (
                        used_prestage
                        and not re.search(r"num=(\d+)", act_text)
                        and self._prestage_rejection_requires_refresh(act_status, act_text)
                    ):
                        self.log(
                            f"[{worker_label}] 사전 준비값이 변경되어 대상 회차 값으로 즉시 재시도합니다.",
                            "warning",
                        )
                        current_stage = "예약 입력 화면 조회"
                        price_fields = await self._read_price_fields_async(
                            booking_session, input_url, worker_label, slot_id
                        )
                        current_stage = "예약 주문 생성"
                        act_status, act_text = await create_order(price_fields)

                    # Parse response to find num
                    num_m = re.search(r"num=(\d+)", act_text)
                    if num_m:
                        num = num_m.group(1)
                        self.log(
                            f"[{worker_label}] [주문 생성] slotId={slot_id} · orderId={num}",
                            "info",
                        )
                        kcp_url = f"{self.base_url}/layout/res/home.php?go=rev.kcp&num={num}"
                        
                        # 4. Fetch KCP page to extract ck_code
                        current_stage = "결제 준비 화면 조회"
                        request_started = time.perf_counter()
                        async with booking_session.get(kcp_url, headers=headers, timeout=8) as kcp_resp:
                            kcp_status = kcp_resp.status
                            kcp_text = await kcp_resp.text(encoding='utf-8', errors='ignore')
                        self._log_http_diagnostic(
                            worker_label,
                            current_stage,
                            "GET",
                            kcp_status,
                            time.perf_counter() - request_started,
                            detail=f"orderId={num}",
                            force=True,
                        )
                        
                        ck_m = re.search(r"name=['\"]?ck_code['\"]?\s*value=['\"]?([^'\"'>\s]+)", kcp_text)
                        ck_code_val = ck_m.group(1) if ck_m else ""

                        # 5. Submit mutong.php (using GET or POST - GET is browser default)
                        mutong_url = f"{self.base_url}/core/res/rev.make.mutong.php"
                        mutong_params = {
                            "num": num,
                            "ck_code": ck_code_val,
                            "layout_folder": "layout/res",
                            "payment": "D"  # 'D' is mutong for Doom Escape
                        }
                        
                        query_str = urllib.parse.urlencode(mutong_params)
                        mutong_get_url = f"{mutong_url}?{query_str}"

                        current_stage = "무통장 예약 확정"
                        request_started = time.perf_counter()
                        async with booking_session.get(mutong_get_url, headers=headers, timeout=8) as mutong_resp:
                            mutong_status = mutong_resp.status
                            mutong_bytes = await mutong_resp.read()
                            mutong_text = mutong_bytes.decode('utf-8', errors='ignore')
                        self._log_http_diagnostic(
                            worker_label,
                            current_stage,
                            "GET",
                            mutong_status,
                            time.perf_counter() - request_started,
                            detail=f"orderId={num}",
                            force=True,
                        )

                        # Follow meta refresh -> rev.make.exe.php
                        refresh_m = re.search(r"url=([^'\"\>]+)", mutong_text, re.I)
                        if refresh_m:
                            next_url = refresh_m.group(1).strip()
                            if not next_url.startswith("http"):
                                next_url = urllib.parse.urljoin(mutong_url, next_url)
                            try:
                                current_stage = "예약 완료 화면 확인"
                                request_started = time.perf_counter()
                                async with booking_session.get(next_url, headers=headers, timeout=8) as exe_resp:
                                    exe_status = exe_resp.status
                                    exe_bytes = await exe_resp.read()
                                    exe_text = exe_bytes.decode('utf-8', errors='ignore')
                                    mutong_text += exe_text
                                self._log_http_diagnostic(
                                    worker_label,
                                    current_stage,
                                    "GET",
                                    exe_status,
                                    time.perf_counter() - request_started,
                                    detail=f"orderId={num}",
                                    force=True,
                                )
                            except Exception as exc:
                                self.log(
                                    f"[{worker_label}] [재시도 불가] {current_stage} · {self._describe_exception(exc)} · 이전 응답으로 결과 판정",
                                    "warning",
                                )

                        # Extract final booking number (ck_code)
                        bnum_m = re.search(r"ck_code=(\d+)", mutong_text)
                        booking_number = bnum_m.group(1) if bnum_m else ""
                        completion_code = booking_number or ck_code_val

                        if "rev.make.exe.php" in mutong_text or "rev.make.end" in mutong_text or "완료" in mutong_text or "성공" in mutong_text:
                            final_msg = (
                                f"[{worker_label}] 예약 최종 완료! 예약번호: {booking_number}"
                                if booking_number
                                else f"[{worker_label}] 예약 최종 완료! 예약번호는 완료 화면에서 확인해주세요."
                            )
                            try:
                                import webbrowser
                                webbrowser.open(f"{self.base_url}/layout/res/home.php?go=rev.make.end&num={num}&ck_code={completion_code}")
                            except Exception:
                                pass
                            try:
                                append_history({
                                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                    "site": "둠이스케이프",
                                    "date": rev_days,
                                    "time": target_time,
                                    "booking_number": booking_number,
                                })
                            except Exception:
                                pass
                        else:
                            self._write_safe_failure_summary(
                                worker=worker_label,
                                stage="무통장 예약 결과 판정",
                                status=mutong_status,
                                response_text=mutong_text,
                                slot_id=slot_id,
                                order_id=num,
                            )
                            final_msg = (
                                f"[{worker_label}] 예약 선점 성공! 예약번호: {booking_number} / 임시번호: {num} "
                                "(결제확인 응답 재확인 필요)"
                                if booking_number
                                else f"[{worker_label}] 예약 선점 성공! 임시번호: {num} (결제확인 응답 재확인 필요)"
                            )
                    else:
                        err_msg = "선점 실패"
                        alert_match = re.search(r"alert\s*\(\s*['\"](.*?)['\"]\s*\)", act_text)
                        if alert_match:
                            err_msg = alert_match.group(1)
                        raise Exception(err_msg)

                    self.log(f"🎉 {final_msg}", "success")
                    self.notify_success()
                    break

                finally:
                    if hasattr(self, "submission_lock"):
                        try:
                            self.submission_lock.release()
                        except RuntimeError:
                            pass

            except Exception as e:
                if self.stop_event.is_set():
                    break
                err_str = self._describe_exception(e)
                now = time.time()
                show_log = False
                with self._log_lock:
                    if err_str != self._last_err_msg or (now - self._last_err_time) > 3.0:
                        self._last_err_msg = err_str
                        self._last_err_time = now
                        show_log = True
                if show_log and not self.stop_event.is_set():
                    retry_text = (
                        "독립 연결로 감시 지속"
                        if current_stage == "시간표 조회" and self._is_transient_site_error(e)
                        else "즉시 재시도"
                    )
                    self.log(
                        f"[{worker_label}] [오류] {current_stage} · {err_str} · {retry_text}",
                        "error",
                    )
                
                if current_stage == "시간표 조회" and self._is_transient_site_error(e):
                    self._note_scan_failure(worker_label, current_stage, e)
                    await asyncio.sleep(0)
                else:
                    await asyncio.sleep(0.05)
                
        is_pooled = hasattr(self, "session_pool") and len(self.session_pool) > 0
        if not is_pooled:
            try:
                await session.close()
            except Exception:
                pass

    async def pre_fetch_sessions_async(self, num_sessions, reservation_data):
        self.scan_governor = DoomScanGovernor(
            self.IDLE_SCAN_RATE_PER_SECOND,
            self.ACTIVE_SCAN_RATE_PER_SECOND,
            self.MIN_SCAN_RATE_PER_SECOND,
            self.ACTIVE_SCAN_FLOOR_PER_SECOND,
        )
        self._scan_inflight = asyncio.Semaphore(self.MAX_SCAN_INFLIGHT)
        self._prestage_lock = asyncio.Lock()
        self._prestaged_prices = None
        self._last_slot_reason = None
        self._open_time_recorded = False
        self._outage_started_at = 0.0
        self._outage_reported = False
        self._last_scan_failure_at = 0.0
        self._inventory_logged = False
        self._unverified_date_warned = False
        self._branch_id = str(reservation_data.get("branch", "3") or "3")
        configured_open = ""
        for key in ("openDateTime", "openAt", "reservationOpenAt", "openTime"):
            if reservation_data.get(key):
                configured_open = str(reservation_data[key])
                break
        learned_open = self.load_learned_open_time(self._branch_id)
        anchor_text = configured_open or learned_open or self.DEFAULT_OPEN_TIME
        self._open_anchor_epoch = self._open_anchor_from_wall_clock(anchor_text)
        self.session_pool = []
        self._slot_wait_started_at = time.time()
        self._scan_session_count = max(1, min(int(num_sessions), self.MAX_SCAN_SESSIONS))
        total_sessions = self._scan_session_count + 1
        self.log(
            f"[정보] 설정 작업 {num_sessions}개 · 실제 감시 연결 {self._scan_session_count}개 · "
            "제출 전용 연결 1개를 준비합니다.",
            "info",
        )
        if self._open_anchor_epoch is not None:
            opens_at = datetime.fromtimestamp(self._open_anchor_epoch)
            self.log(
                f"[정보] 집중 감시 예정 {opens_at.strftime('%Y-%m-%d %H:%M:%S')} · "
                f"{self.OPEN_ANCHOR_LEAD_SECONDS:.0f}초 전부터 {self.ACTIVE_SCAN_RATE_PER_SECOND:.0f}회/초 · "
                f"응답 대기 최대 {self.MAX_SCAN_INFLIGHT}개",
                "info",
            )
        home_url = f"{self.base_url}/layout/res/home.php?go=main"
        warm_gate = asyncio.Semaphore(self.MAX_WARM_INFLIGHT)

        async def warm_one():
            session = aiohttp.ClientSession(headers=self.REQUEST_HEADERS)
            warmed = False
            status = None
            error = ""
            started = time.perf_counter()
            try:
                async with warm_gate:
                    async with session.get(
                        home_url, timeout=self.REQUEST_TIMEOUT_SECONDS
                    ) as response:
                        await response.read()
                        status = response.status
                        warmed = response.status == 200
            except Exception as exc:
                error = self._describe_exception(exc)
            return session, warmed, status, time.perf_counter() - started, error

        results = await asyncio.gather(*(warm_one() for _ in range(total_sessions)))
        self.session_pool = [session for session, _warmed, _status, _rtt, _error in results]
        self._submit_session = self.session_pool[-1]
        warmed_count = sum(
            1 for _session, warmed, _status, _rtt, _error in results if warmed
        )
        status_counts = {}
        for _session, _warmed, status, _rtt, error in results:
            label = str(status) if status is not None else (error or "연결 실패")
            status_counts[label] = status_counts.get(label, 0) + 1
        status_summary = ", ".join(
            f"{key}={value}" for key, value in sorted(status_counts.items())
        )
        max_rtt = max((rtt for _session, _warmed, _status, rtt, _error in results), default=0.0)
        if warmed_count == total_sessions:
            self.log(
                f"[정보] 둠이스케이프 연결 {warmed_count}/{total_sessions}개 제한 병렬 예열 완료 · "
                f"HTTP {status_summary} · 최대 RTT {max_rtt * 1000:.0f}ms",
                "info",
            )
        else:
            self.log(
                f"[경고] 둠이스케이프 연결 예열 {warmed_count}/{total_sessions}개 완료 · "
                f"HTTP {status_summary} · 최대 RTT {max_rtt * 1000:.0f}ms · "
                "정상 연결부터 감시를 시작합니다.",
                "warning",
            )
