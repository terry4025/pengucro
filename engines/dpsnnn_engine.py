from __future__ import annotations

import asyncio
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup

from engines import browser_session
from engines.base_engine import BaseEngine
from engines.zeroworld_catalog import ZeroWorldTimeSlot
from pengucro.diagnostics import format_exception
from pengucro.models import BookingResult, parse_bool_flag
from engines.dpsnnn_runtime import DpsnnnSession, KST, WarmCheckout
from engines.dpsnnn_orders import OrderJournal


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DPSNNN_MAX_WORKERS = 4
DPSNNN_DEFAULT_WORKERS = 4
DPSNNN_PAYMENT_FORM_SELECTOR = "form#order_payment"
DPSNNN_PAYMENT_READY_SELECTOR = f'{DPSNNN_PAYMENT_FORM_SELECTOR}[data-init="Y"]'
DPSNNN_ORDERER_NAME_SELECTOR = (
    f'{DPSNNN_PAYMENT_FORM_SELECTOR} input[name="orderer_name"]'
)
DPSNNN_ORDERER_CALL_SELECTOR = (
    f'{DPSNNN_PAYMENT_FORM_SELECTOR} input[name="orderer_call"]'
)
DPSNNN_ORDERER_EDIT_SELECTOR = f"{DPSNNN_PAYMENT_FORM_SELECTOR} ._btn_orderer_edit"
DPSNNN_CASH_SELECTOR = f'{DPSNNN_PAYMENT_FORM_SELECTOR} input[name="pay_type"][value="cash"]'
DPSNNN_DEPOSITOR_SELECTOR = f"{DPSNNN_PAYMENT_FORM_SELECTOR} input#depositor_name"
DPSNNN_CANCEL_AGREE_SELECTOR = (
    f'{DPSNNN_PAYMENT_FORM_SELECTOR} input[name="agree_cancel"]'
)
DPSNNN_ALL_AGREE_SELECTOR = f"{DPSNNN_PAYMENT_FORM_SELECTOR} #paymentAllCheck"
DPSNNN_REQUIRED_AGREE_SELECTOR = f"{DPSNNN_PAYMENT_FORM_SELECTOR} input._agree"
DPSNNN_SUBMIT_SELECTORS = (
    f"{DPSNNN_PAYMENT_FORM_SELECTOR} a._btn_start_payment",
    f"{DPSNNN_PAYMENT_FORM_SELECTOR} ._btn_start_payment",
    f'{DPSNNN_PAYMENT_FORM_SELECTOR} [role="button"]',
    f'{DPSNNN_PAYMENT_FORM_SELECTOR} button',
    f'{DPSNNN_PAYMENT_FORM_SELECTOR} input[type="submit"]',
)

DPSNNN_BRANCHES: dict[str, dict[str, Any]] = {
    "gangnam": {
        "name": "강남",
        "base_url": "https://www.dpsnnn.com",
        "reserve_path": "/reserve_g",
        "menu_code": "m2021111422e8d3a51ef50",
        "themes": {
            "상자": "그림자 없는 상자",
            "행복": "사람들은 그것을 행복이라 부르기로 했다",
        },
    },
    "seongsu": {
        "name": "성수",
        "base_url": "https://dpsnnn-s.imweb.me",
        "reserve_path": "/reserve_ss",
        "menu_code": "m20240510757d084f8aa01",
        "themes": {
            "문장": "쓰여진 문장 속에 구원이 없다면",
            "자격": "존재할 자격",
            "쥐": "쥐와 파시스트와 마지막 한 장",
            "별": "뱃사람의 별",
        },
    },
}


def resolve_dpsnnn_branch(
    branch_id: str,
    engine_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    branch_id = str(branch_id)
    configured = dict((engine_options or {}).get("branches", {}).get(branch_id, {}))
    defaults = DPSNNN_BRANCHES.get(branch_id)
    if defaults is None and not configured:
        raise ValueError(f"알 수 없는 단편선 지점입니다: {branch_id}")
    result = {**(defaults or {}), **configured, "id": branch_id}
    result["base_url"] = str(result.get("base_url", "")).rstrip("/")
    result["reserve_path"] = "/" + str(result.get("reserve_path", "")).lstrip("/")
    if not result["base_url"] or not result.get("menu_code"):
        raise ValueError("단편선 지점의 예약 주소 또는 메뉴 코드가 없습니다.")
    return result


def calculate_dpsnnn_open_datetime(date_str: str) -> datetime:
    """Official rule: each date opens at midnight six calendar days earlier."""
    target = datetime.strptime(date_str, "%Y-%m-%d")
    return (target - timedelta(days=6)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def create_dpsnnn_session() -> requests.Session:
    session = DpsnnnSession()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return session


def parse_dpsnnn_calendar(
    html: str,
    date_str: str,
    theme_alias: str = "",
) -> list[ZeroWorldTimeSlot]:
    target_day = date_str.replace("-", "")
    slots: list[ZeroWorldTimeSlot] = []
    seen: set[tuple[str, str]] = set()
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        parsed = urllib.parse.urlparse(str(anchor.get("href", "")))
        query = urllib.parse.parse_qs(parsed.query)
        slot_id = str(query.get("idx", [""])[0])
        day = str(query.get("day", [""])[0])
        if not slot_id or day != target_day:
            continue

        label_node = anchor.select_one(".text")
        label = label_node.get_text(" ", strip=True) if label_node else anchor.get_text(" ", strip=True)
        match = re.search(r"([^/]+?)\s*/\s*(\d{1,2}:\d{2})", label)
        if not match:
            continue
        alias = re.sub(r"\s+", "", match.group(1))
        if theme_alias and alias.casefold() != re.sub(r"\s+", "", theme_alias).casefold():
            continue
        slot_time = match.group(2).zfill(5)
        key = (slot_time, slot_id)
        if key in seen:
            continue
        seen.add(key)

        row = anchor.find_parent("div", class_=lambda value: value and "booking_list" in value)
        classes = {str(item).casefold() for item in (row.get("class", []) if row else [])}
        onclick = str(anchor.get("onclick", "")).casefold()
        badge = anchor.select_one(".booking_badge, .badge")
        badge_text = badge.get_text(" ", strip=True) if badge else ""
        unavailable = bool(classes.intersection({"waiting", "closed", "disable", "disabled"}))
        unavailable = unavailable or "return false" in onclick or badge_text not in {"가", "예약가능"}
        slots.append(ZeroWorldTimeSlot(slot_time, slot_id, not unavailable))
    return sorted(slots, key=lambda item: (item.time, item.slot_id))


def fetch_exact_dpsnnn_slots(
    session: requests.Session,
    branch: dict[str, Any],
    theme_alias: str,
    date_str: str,
    timeout: float = 8.0,
    diagnostics: Callable[[str, str, int | None, float, str], None] | None = None,
) -> list[ZeroWorldTimeSlot]:
    reserve_url = urllib.parse.urljoin(branch["base_url"] + "/", branch["reserve_path"].lstrip("/"))
    started = time.perf_counter()
    response = None
    try:
        response = session.post(
            urllib.parse.urljoin(branch["base_url"] + "/", "booking/html_list.cm"),
            data={
                "target_month": date_str[:7],
                "select_day": date_str,
                "menu_code": branch["menu_code"],
            },
            headers={"Referer": reserve_url, "Origin": branch["base_url"]},
            timeout=timeout,
        )
        if diagnostics is not None:
            diagnostics(
                "시간표 조회",
                "POST",
                getattr(response, "status_code", None),
                time.perf_counter() - started,
                "",
            )
        response.raise_for_status()
        return parse_dpsnnn_calendar(response.text, date_str, theme_alias)
    except Exception as exc:
        if diagnostics is not None and response is None:
            diagnostics(
                "시간표 조회",
                "POST",
                None,
                time.perf_counter() - started,
                DpsnnnEngine._describe_exception(exc),
            )
        raise


def fetch_dpsnnn_slots(
    branch_id: str,
    theme_alias: str,
    date_str: str,
    engine_options: dict[str, Any] | None = None,
    timeout: float = 8.0,
    *,
    session: requests.Session | None = None,
) -> list[ZeroWorldTimeSlot]:
    branch = resolve_dpsnnn_branch(branch_id, engine_options)
    own_session = session is None
    session = session or create_dpsnnn_session()
    try:
        reserve_url = urllib.parse.urljoin(branch["base_url"] + "/", branch["reserve_path"].lstrip("/"))
        session.get(reserve_url, timeout=timeout).raise_for_status()
        exact = fetch_exact_dpsnnn_slots(session, branch, theme_alias, date_str, timeout)
        if exact:
            return exact

        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        for weeks in range(1, 9):
            source = target - timedelta(days=weeks * 7)
            template = fetch_exact_dpsnnn_slots(
                session, branch, theme_alias, source.isoformat(), timeout
            )
            if template:
                return [
                    ZeroWorldTimeSlot(
                        time=item.time,
                        slot_id="",
                        available=False,
                        estimated=True,
                        source_date=source.isoformat(),
                        estimate_basis="same_weekday",
                    )
                    for item in template
                ]
        return []
    finally:
        if own_session:
            session.close()


def _hidden_form_values(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one("#booking_f") or soup.find("form")
    values: dict[str, str] = {}
    container = form or soup
    for field in container.select("input[name]"):
        values[str(field.get("name"))] = str(field.get("value", ""))
    return values


class DpsnnnEngine(BaseEngine):
    """단편선의 Imweb 예약 모듈을 위한 전용 엔진."""

    POLL_INTERVAL = 0.04
    WORKER_STAGGER = 0.20
    REQUEST_TIMEOUT = 8.0
    PAYLOAD_PRESTAGE_SECONDS = 3.0
    PAYLOAD_MAX_AGE_SECONDS = 15.0

    def __init__(
        self,
        log_callback,
        success_callback=None,
        *,
        site_url: str = "",
        engine_options: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(log_callback, success_callback)
        self.site_url = site_url
        self.engine_options = dict(engine_options or {})
        self._order_claimed = threading.Event()
        self._worker_index_lock = threading.Lock()
        self._next_worker_index = 0
        self._diagnostic_log_lock = threading.Lock()
        self._diagnostic_log_state: dict[tuple[str, ...], dict[str, float | int]] = {}
        self._warm_checkout = None
        self._journal = None
        self._start_lock = threading.Lock()

    @staticmethod
    def _describe_exception(exc: BaseException) -> str:
        return format_exception(exc)

    def _log_http_diagnostic(
        self,
        worker: str,
        stage: str,
        method: str,
        status: int | None,
        elapsed_seconds: float,
        detail: str = "",
        *,
        force: bool = False,
    ) -> None:
        """Log bounded HTTP metadata without request or response contents."""
        status_text = str(status) if status is not None else "연결 실패"
        detail_text = str(detail).strip()
        key = (worker, stage, method, status_text, detail_text)
        now = time.monotonic()
        with self._diagnostic_log_lock:
            state = self._diagnostic_log_state.setdefault(
                key, {"last": 0.0, "count": 0}
            )
            state["count"] = int(state["count"]) + 1
            should_log = force or not state["last"] or now - float(state["last"]) >= 5.0
            if not should_log:
                return
            attempts = int(state["count"])
            state["last"] = now
            state["count"] = 0

        repeat = f" · 최근 {attempts}회" if attempts > 1 else ""
        suffix = f" · {detail_text}" if detail_text else ""
        level = "info" if status_text.startswith("2") else "warning"
        self.log(
            f"[{worker}] [HTTP] {stage} · {method} · status={status_text} · "
            f"RTT {max(0.0, elapsed_seconds) * 1000:.0f}ms{repeat}{suffix}",
            level,
        )

    def start_reservation(self, reservation_data, num_threads, is_async=False) -> None:
        with self._start_lock:
            if self.is_running or (self._warm_checkout is not None and not self._warm_checkout.finished.is_set()):
                self.log("예약 엔진이 이미 실행 중입니다.", "warning")
                return
            self._start_reservation(reservation_data, num_threads, is_async)

    def _start_reservation(self, reservation_data, num_threads, is_async=False) -> None:
        workers = max(1, min(int(num_threads), DPSNNN_MAX_WORKERS))
        self._warm_checkout = None
        self._journal = None
        self._order_claimed.clear()
        with self._worker_index_lock:
            self._next_worker_index = 0
        with self._diagnostic_log_lock:
            self._diagnostic_log_state.clear()
        try:
            branch = resolve_dpsnnn_branch(
                str(reservation_data.get("branch", "")), self.engine_options
            )
            alias = self._theme_alias(branch, reservation_data)
            self.log(
                f"단편선 예약 준비 · {branch['name']} · "
                f"{branch['themes'].get(alias, alias)} · "
                f"{reservation_data.get('reservationDate', '')} "
                f"{str(reservation_data.get('reservationTime', ''))[:5]}",
                "info",
            )
            open_at = calculate_dpsnnn_open_datetime(
                str(reservation_data.get("reservationDate", ""))
            )
            self.log(
                f"[정보] 선택 날짜 오픈 예정 · "
                f"{open_at.strftime('%Y-%m-%d %H:%M')} (단편선 안내 규칙)",
                "info",
            )
        except (KeyError, ValueError):
            pass
        self.log(
            f"단편선 모드: {workers}개 독립 세션이 병렬로 슬롯을 감시합니다. "
            "주문 생성은 먼저 통과한 1개 세션만 실행합니다.",
            "info",
        )
        if not parse_bool_flag(reservation_data.get("devMode", False)):
            branch = resolve_dpsnnn_branch(str(reservation_data.get("branch", "")), self.engine_options)
            if not str(reservation_data.get("name", "")).strip() or len(re.sub(r"\D", "", str(reservation_data.get("phone", "")))) not in (10, 11):
                raise ValueError("예약자 이름과 연락처를 확인해주세요.")
            self._journal = OrderJournal(reservation_data)
            if self._journal.read() is not None:
                self._order_claimed.set()
                self.log("이 예약의 기존 주문 기록이 있습니다. 중복 제출을 막았습니다. 기존 예약번호·결제 화면·알림톡을 먼저 확인해주세요.", "warning")
                return
            self.stop_event.clear()
            self._warm_checkout = WarmCheckout(branch, reservation_data, self.log, self.stop_event)
            self._warm_checkout.start()
        super().start_reservation(reservation_data, workers, is_async=False)

    def get_csrf_token(self, session, url=None) -> str:
        return ""

    def _remember_order(self, state, order_code="", booking_number=""):
        if self._journal is not None:
            try:
                self._journal.update(state, order_code, booking_number)
            except OSError:
                # The durable pre-submit claim still prevents another order.
                self.log("주문 상태 추가 저장 실패 · 기존 중복방지 기록을 유지합니다.", "warning")

    def _theme_alias(self, branch: dict[str, Any], reservation_data: dict[str, Any]) -> str:
        raw = str(reservation_data.get("themePK", "")).strip()
        metadata = reservation_data.get("engine_metadata", {})
        theme_meta = metadata.get("theme", {}) if isinstance(metadata, dict) else {}
        return str(theme_meta.get("alias") or raw).strip()

    def _build_order_payload(
        self,
        session: requests.Session,
        branch: dict[str, Any],
        slot_id: str,
        date_str: str,
        worker_label: str = "주문 작업",
    ) -> dict[str, str]:
        day_compact = date_str.replace("-", "")
        detail_url = urllib.parse.urljoin(branch["base_url"] + "/", branch["reserve_path"].lstrip("/"))
        started = time.perf_counter()
        detail = session.get(
            detail_url,
            params={"idx": slot_id, "day": day_compact},
            timeout=self.REQUEST_TIMEOUT,
        )
        self._log_http_diagnostic(
            worker_label,
            "예약 상세 조회",
            "GET",
            getattr(detail, "status_code", None),
            time.perf_counter() - started,
            f"slotId={slot_id}",
            force=True,
        )
        detail.raise_for_status()
        values = _hidden_form_values(detail.text)

        started = time.perf_counter()
        calendar = session.post(
            urllib.parse.urljoin(branch["base_url"] + "/", "shop/load_booking_detail_detail_calendar.cm"),
            data={"idx": slot_id, "start_day": day_compact, "end_day": date_str},
            headers={"Referer": detail.url, "Origin": branch["base_url"]},
            timeout=self.REQUEST_TIMEOUT,
        )
        self._log_http_diagnostic(
            worker_label,
            "예약 상세 일정 조회",
            "POST",
            getattr(calendar, "status_code", None),
            time.perf_counter() - started,
            f"slotId={slot_id}",
            force=True,
        )
        calendar.raise_for_status()
        calendar_payload = calendar.json()
        values.update(_hidden_form_values(str(calendar_payload.get("output", ""))))
        values.setdefault("prod_idx", slot_id)
        values.setdefault("backurl", detail.url)
        values["unselected_end_day"] = "false"
        required = {"prod_idx", "start_day", "start_timestamp", "end_day", "end_timestamp"}
        if not required.issubset(values):
            missing = ", ".join(sorted(required.difference(values)))
            raise ValueError(f"예약 주문 필드가 부족합니다: {missing}")
        if not self._prepared_payload_usable(values, slot_id, 0.0,
                                              date_str=date_str, now_monotonic=0.0):
            raise ValueError("예약 상세의 날짜·상품·시간 필드가 목표와 일치하지 않습니다.")
        return values

    @classmethod
    def _payload_prestage_due(
        cls,
        date_str: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(KST).replace(tzinfo=None)
        if current.tzinfo is not None:
            current = current.astimezone(KST).replace(tzinfo=None)
        seconds_until_open = (
            calculate_dpsnnn_open_datetime(date_str) - current
        ).total_seconds()
        return (
            -cls.PAYLOAD_MAX_AGE_SECONDS
            <= seconds_until_open
            <= cls.PAYLOAD_PRESTAGE_SECONDS
        )

    @classmethod
    def _prepared_payload_usable(
        cls,
        payload: dict[str, str] | None,
        slot_id: str,
        prepared_at: float,
        *,
        date_str: str = "",
        now_monotonic: float | None = None,
    ) -> bool:
        if not payload or str(payload.get("prod_idx", "")) != str(slot_id):
            return False
        required = ("start_day", "start_timestamp", "end_day", "end_timestamp")
        if any(not str(payload.get(field, "")).strip() for field in required):
            return False
        try:
            start_timestamp = int(str(payload["start_timestamp"]))
            end_timestamp = int(str(payload["end_timestamp"]))
            if start_timestamp <= 0 or end_timestamp < start_timestamp:
                return False
        except (TypeError, ValueError):
            return False
        if date_str:
            target_day = date_str.replace("-", "")
            start_day = str(payload["start_day"]).replace("-", "")
            end_day = str(payload["end_day"]).replace("-", "")
            if start_day != target_day or end_day != target_day:
                return False
            try:
                if any(datetime.fromtimestamp(stamp, KST).strftime("%Y%m%d") != target_day
                       for stamp in (start_timestamp, end_timestamp)):
                    return False
            except (OverflowError, OSError, ValueError):
                return False
        current = time.monotonic() if now_monotonic is None else now_monotonic
        age = current - prepared_at
        return 0.0 <= age <= cls.PAYLOAD_MAX_AGE_SECONDS

    def _add_order(
        self,
        session: requests.Session,
        branch: dict[str, Any],
        payload: dict[str, str],
        worker_label: str = "주문 작업",
    ) -> tuple[str, str]:
        started = time.perf_counter()
        response = session.post(
            urllib.parse.urljoin(branch["base_url"] + "/", "booking/add_order.cm"),
            data=payload,
            headers={
                "Referer": payload.get("backurl", branch["base_url"]),
                "Origin": branch["base_url"],
            },
            timeout=self.REQUEST_TIMEOUT,
        )
        slot_id = str(payload.get("prod_idx", ""))
        self._log_http_diagnostic(
            worker_label,
            "예약 주문 생성",
            "POST",
            getattr(response, "status_code", None),
            time.perf_counter() - started,
            f"slotId={slot_id}" if slot_id else "",
            force=True,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("주문 응답 형식이 불명확합니다.")
        message = str(data.get("msg", ""))
        order_code = str(data.get("order_code", ""))
        if message.upper() == "SUCCESS" and order_code:
            return order_code, message
        if not message or message.upper() == "SUCCESS":
            raise ValueError("주문 생성 여부를 확인할 수 없습니다.")
        return "", message

    @staticmethod
    def _fill_first(page, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 8)
                for index in range(count):
                    field = locator.nth(index)
                    if field.is_visible() and field.is_enabled():
                        if field.evaluate("element => element.tagName") == "SELECT":
                            field.select_option(value)
                        else:
                            field.fill(value)
                        if field.input_value().strip() != value.strip():
                            continue
                        return True
            except Exception:
                continue
        return False

    @staticmethod
    def _checked(locator) -> bool:
        try:
            return bool(locator.is_checked())
        except Exception:
            return False

    @classmethod
    def _activate_labeled_control(cls, page, control, timeout_ms: int = 2500) -> bool:
        """Activate an Imweb styled radio/checkbox through its real label."""
        control.wait_for(state="attached", timeout=timeout_ms)
        if cls._checked(control):
            return True

        label = control.locator("xpath=ancestor::label[1]")
        try:
            if label.count() and label.is_visible():
                label.click(force=True)
        except Exception:
            pass

        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            if cls._checked(control):
                return True
            page.wait_for_timeout(25)

        # Defensive fallback for a future template without a clickable label.
        # Dispatching change is required because Imweb keeps its payment state
        # in JavaScript rather than reading only the input at submit time.
        try:
            control.evaluate(
                """element => {
                    element.checked = true;
                    element.dispatchEvent(new Event('input', {bubbles: true}));
                    element.dispatchEvent(new Event('change', {bubbles: true}));
                }"""
            )
        except Exception:
            return False

        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            if cls._checked(control):
                return True
            page.wait_for_timeout(25)
        return False

    @classmethod
    def _prepare_orderer_checkout(
        cls,
        page,
        orderer_name: str,
        orderer_phone: str,
    ) -> tuple[bool, str]:
        """Fill Imweb's dynamically rendered booking contact fields."""

        normalized_name = orderer_name.strip()
        phone_digits = "".join(character for character in orderer_phone if character.isdigit())
        if len(normalized_name) < 2:
            return False, "예약자 이름이 올바르지 않습니다."
        if len(phone_digits) not in (10, 11):
            return False, "예약자 연락처가 올바르지 않습니다."

        try:
            # Imweb inserts these inputs from TEMPLATE_ORDERER_EDIT_WRAP only
            # after site_payment.js finishes initialization. Looking for a
            # generic name/tel input before data-init=Y silently misses both.
            page.wait_for_selector(
                DPSNNN_PAYMENT_READY_SELECTOR,
                state="attached",
                timeout=15000,
            )
            name_field = page.locator(DPSNNN_ORDERER_NAME_SELECTOR).first
            phone_field = page.locator(DPSNNN_ORDERER_CALL_SELECTOR).first
            name_field.wait_for(state="attached", timeout=10000)
            phone_field.wait_for(state="attached", timeout=10000)
            if not name_field.is_visible() or not phone_field.is_visible():
                edit_button = page.locator(DPSNNN_ORDERER_EDIT_SELECTOR).first
                if edit_button.count() and edit_button.is_visible():
                    edit_button.click(force=True)
            name_field.wait_for(state="visible", timeout=10000)
            phone_field.wait_for(state="visible", timeout=10000)

            name_field.fill(normalized_name)
            phone_field.fill(orderer_phone.strip())
            for field in (name_field, phone_field):
                field.evaluate(
                    """element => {
                        element.dispatchEvent(new Event('change', {bubbles: true}));
                        element.blur();
                    }"""
                )

            page.wait_for_function(
                r"""expected => {
                    const name = document.querySelector(expected.nameSelector);
                    const phone = document.querySelector(expected.phoneSelector);
                    const nameWrap = name && name.closest('._orderer_name_wrap');
                    const phoneWrap = phone && phone.closest('._orderer_call_wrap');
                    return Boolean(
                        name && phone &&
                        name.value.trim() === expected.name &&
                        phone.value.replace(/\D/g, '') === expected.phoneDigits &&
                        (!nameWrap || nameWrap.getAttribute('data-error') !== 'Y') &&
                        (!phoneWrap || phoneWrap.getAttribute('data-error') !== 'Y')
                    );
                }""",
                arg={
                    "nameSelector": DPSNNN_ORDERER_NAME_SELECTOR,
                    "phoneSelector": DPSNNN_ORDERER_CALL_SELECTOR,
                    "name": normalized_name,
                    "phoneDigits": phone_digits,
                },
                timeout=5000,
            )

            if name_field.input_value().strip() != normalized_name:
                return False, "예약자 이름을 입력하지 못했습니다."
            actual_phone_digits = "".join(
                character
                for character in phone_field.input_value()
                if character.isdigit()
            )
            if actual_phone_digits != phone_digits:
                return False, "예약자 연락처를 입력하지 못했습니다."
            return True, ""
        except Exception as exc:
            return False, f"예약자 정보 입력 실패: {cls._describe_exception(exc)}"

    @classmethod
    def _prepare_cash_checkout(cls, page, depositor_name: str) -> tuple[bool, str]:
        """Prepare Imweb's booking checkout through its real form controls."""
        try:
            page.wait_for_selector(
                DPSNNN_PAYMENT_FORM_SELECTOR,
                state="attached",
                timeout=15000,
            )

            # The form is server-rendered before site_payment.js finishes
            # binding its change handlers and selecting the default pay type.
            page.wait_for_function(
                """selector => {
                    const form = document.querySelector(selector);
                    return Boolean(
                        form &&
                        form.getAttribute('data-pay-type') &&
                        form.querySelector('input[name="pay_type"]:checked')
                    );
                }""",
                arg=DPSNNN_PAYMENT_FORM_SELECTOR,
                timeout=10000,
            )

            cash = page.locator(DPSNNN_CASH_SELECTOR).first
            if not cls._activate_labeled_control(page, cash):
                return False, "무통장입금 선택이 적용되지 않았습니다."
            page.wait_for_function(
                """selector => {
                    const radio = document.querySelector(selector);
                    const form = radio && radio.closest('form');
                    return Boolean(
                        radio && radio.checked && form &&
                        form.getAttribute('data-pay-type') === 'cash'
                    );
                }""",
                arg=DPSNNN_CASH_SELECTOR,
                timeout=5000,
            )

            depositor = page.locator(DPSNNN_DEPOSITOR_SELECTOR).first
            depositor.wait_for(state="visible", timeout=10000)
            depositor.fill(depositor_name)
            if depositor.input_value().strip() != depositor_name.strip():
                return False, "무통장입금 입금자명을 입력하지 못했습니다."

            cancel_agree = page.locator(DPSNNN_CANCEL_AGREE_SELECTOR).first
            if not cls._activate_labeled_control(page, cancel_agree):
                return False, "취소·환불 규정 동의가 적용되지 않았습니다."

            all_agree = page.locator(DPSNNN_ALL_AGREE_SELECTOR).first
            if not cls._activate_labeled_control(page, all_agree):
                return False, "결제 약관 전체 동의가 적용되지 않았습니다."

            required = page.locator(DPSNNN_REQUIRED_AGREE_SELECTOR)
            required_count = required.count()
            if required_count == 0:
                return False, "결제 필수 약관 항목을 찾지 못했습니다."
            for index in range(required_count):
                checkbox = required.nth(index)
                if not cls._activate_labeled_control(page, checkbox):
                    return False, "결제 필수 약관 전체 동의가 적용되지 않았습니다."

            if not cls._checked(cancel_agree):
                return False, "취소·환불 규정 동의가 적용되지 않았습니다."
            if any(not cls._checked(required.nth(index)) for index in range(required_count)):
                return False, "결제 필수 약관 전체 동의가 적용되지 않았습니다."
            return True, ""
        except Exception as exc:
            return False, f"결제 정보 입력 실패: {cls._describe_exception(exc)}"

    @staticmethod
    def _find_checkout_submit(page):
        for selector in DPSNNN_SUBMIT_SELECTORS:
            candidates = page.locator(selector)
            try:
                for index in range(min(candidates.count(), 12)):
                    candidate = candidates.nth(index)
                    label = (candidate.inner_text().strip() or
                             candidate.get_attribute("value") or
                             candidate.get_attribute("aria-label") or "")
                    if (
                        candidate.is_visible()
                        and candidate.is_enabled()
                        and re.search(r"(?:결제|예약)\s*하기", label)
                        and not re.search(r"취소|돌아가기", label)
                    ):
                        return candidate
            except Exception:
                continue
        return None

    @staticmethod
    def _checkout_success(body: str, current_url: str, payment_url: str) -> bool:
        current_path = urllib.parse.urlparse(current_url).path.rstrip("/").casefold()
        payment_path = urllib.parse.urlparse(payment_url).path.rstrip("/").casefold()
        if urllib.parse.urlparse(current_url).netloc != urllib.parse.urlparse(payment_url).netloc:
            return False
        if re.search(r"(?:예약|주문|결제)\s*(?:실패|불가)|오류가\s*발생", body):
            return False
        return bool(
            current_path
            and current_path != payment_path
            and current_path == "/shop_payment_complete"
            and re.search(r"(?:예약|주문)(?:이|이\s*정상적으로|이\s*성공적으로)?\s*(?:완료|접수)", body)
        )

    @staticmethod
    def _checkout_number(
        body: str,
        order_code: str,
        current_url: str = "",
        response_body: str = "",
    ) -> str:
        candidates: list[str] = []
        for text in (body, response_body):
            for pattern in (
                r"(?:예약|주문)\s*번호\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_-]{5,})",
                r'''["'](?:order_no|order_number|booking_no|booking_number)["']\s*:\s*["']([^"']+)["']''',
            ):
                candidates.extend(match.group(1) for match in re.finditer(pattern, text, re.I))

        query = urllib.parse.parse_qs(urllib.parse.urlparse(current_url).query)
        for key in ("order_no", "order_number", "booking_no", "booking_number"):
            candidates.extend(str(value) for value in query.get(key, ()))

        internal = order_code.strip().casefold()
        for candidate in candidates:
            normalized = str(candidate).strip()
            if (
                len(normalized) >= 6
                and normalized.casefold() != internal
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]+", normalized)
            ):
                return normalized
        return ""

    @staticmethod
    def _release_browser_lease_when_closed(chrome) -> None:
        def wait_for_close() -> None:
            while browser_session.cdp_descriptor(chrome.port):
                time.sleep(0.5)
            chrome.release()

        threading.Thread(
            target=wait_for_close,
            name=f"DpsnnnChromeRelease-{chrome.port}",
            daemon=True,
        ).start()

    def _complete_checkout(self, session, branch, order_code, reservation_data,
                           worker_label="주문 작업") -> tuple[bool, str]:
        if self._warm_checkout is None:
            self._warm_checkout = WarmCheckout(branch, reservation_data, self.log, self.stop_event)
            self._warm_checkout.start()
        return self._warm_checkout.submit(
            lambda context, page: self._checkout_on_page(
                page, context, session, branch, order_code, reservation_data, worker_label))

    def _checkout_on_page(self, page, context, session, branch, order_code,
                          reservation_data, worker_label) -> tuple[bool, str]:
        payment_url = urllib.parse.urljoin(
            branch["base_url"] + "/", f"shop_payment/?order_code={urllib.parse.quote(order_code)}")
        cookies = [{"name": cookie.name, "value": cookie.value,
                    "domain": cookie.domain or urllib.parse.urlparse(branch["base_url"]).hostname,
                    "path": cookie.path or "/"} for cookie in session.cookies]
        if cookies:
            context.add_cookies(cookies)
        try:
            page_started = time.perf_counter()
            navigation = page.goto(
                payment_url, wait_until="domcontentloaded", timeout=30000
            )
            self._log_http_diagnostic(
                worker_label,
                "결제 화면 조회",
                "GET",
                getattr(navigation, "status", None),
                time.perf_counter() - page_started,
                f"orderId={order_code}",
                force=True,
            )
            page.wait_for_selector(
                DPSNNN_PAYMENT_FORM_SELECTOR,
                state="attached",
                timeout=15000,
            )

            name = str(reservation_data.get("name", ""))
            phone = str(reservation_data.get("phone", ""))
            people = str(reservation_data.get("people", ""))
            people_selectors = [
                'input[name*="people" i]', 'select[name*="people" i]',
                'input[placeholder*="인원"]',
            ]
            if any(page.locator(selector).count() for selector in people_selectors):
                if not self._fill_first(page, people_selectors, people):
                    return False, "인원 입력을 확인하지 못했습니다. 열린 화면을 확인해주세요."

            orderer_prepared, orderer_error = self._prepare_orderer_checkout(
                page,
                name,
                phone,
            )
            if not orderer_prepared:
                return False, orderer_error

            prepared, prepare_error = self._prepare_cash_checkout(page, name)
            if not prepared:
                return False, prepare_error

            self.log(
                f"[{worker_label}] [결제 준비] 예약자 이름·연락처 · 무통장입금 · 입금자명 · "
                "취소·환불 및 결제 필수 약관 확인 완료",
                "info",
            )

            if parse_bool_flag(reservation_data.get("devMode", False)):
                self.log(
                    f"[개발자 모드] 결제 전 임시 주문 생성 완료 · 내부 orderId {order_code} · "
                    "최종 결제 클릭 전 정지합니다.",
                    "success",
                )
                return False, "개발자 모드로 결제 직전에 정지했습니다."

            submit = self._find_checkout_submit(page)
            submit_ready_deadline = time.monotonic() + 2.0
            while submit is None and time.monotonic() < submit_ready_deadline:
                page.wait_for_timeout(50)
                submit = self._find_checkout_submit(page)
            if submit is None:
                return False, (
                    "결제 전 임시 주문은 생성했지만 최종 결제 버튼을 찾지 못했습니다. "
                    "열린 화면에서 완료해주세요."
                )

            validation_dialogs: list[str] = []

            def record_dialog(dialog) -> None:
                validation_dialogs.append(str(dialog.message))
                dialog.dismiss()

            page.on("dialog", record_dialog)
            final_responses = []

            def record_final_response(response) -> None:
                if "/backpg/payment/booking/index.cm" in response.url:
                    final_responses.append(response)

            page.on("response", record_final_response)
            check_response = None
            submit_started = time.perf_counter()
            if self.stop_event.is_set():
                return False, "중지 요청으로 최종 제출 전 정지했습니다. 생성된 주문 화면을 확인해주세요."
            try:
                with page.expect_response(
                    lambda response: "/shop/check_payment.cm" in response.url,
                    timeout=12000,
                ) as response_info:
                    submit.click()
                check_response = response_info.value
            except Exception as exc:
                if validation_dialogs:
                    return False, f"최종 결제 검증 실패: {validation_dialogs[-1]}"
                return False, (
                    "결제하기 클릭 후 서버 확인 요청을 받지 못했습니다: "
                    f"{self._describe_exception(exc)}"
                )

            check_status = getattr(check_response, "status", None)
            self.log(
                f"[{worker_label}] [HTTP] 최종 결제 사전 확인 · POST · "
                f"status={check_status if check_status is not None else '없음'} · "
                f"orderId={order_code}",
                "info" if check_status is not None and check_status < 400 else "warning",
            )
            try:
                check_payload = check_response.json()
            except Exception:
                check_payload = {}
            check_message = str(check_payload.get("msg", ""))
            if check_status is None or not 200 <= check_status < 300 or check_message.upper() != "SUCCESS":
                return False, "최종 결제 서버 확인 실패 또는 응답 불명확 · 열린 화면을 확인해주세요."

            self.log(
                f"[{worker_label}] [최종 제출] 결제 사전 확인 완료 · "
                f"orderId={order_code} · 실제 예약 접수 응답 대기",
                "info",
            )

            final_deadline = time.monotonic() + 20.0
            while not final_responses and time.monotonic() < final_deadline:
                if validation_dialogs:
                    return False, f"최종 결제 검증 실패: {validation_dialogs[-1]}"
                page.wait_for_timeout(50)
            if not final_responses:
                return False, (
                    "결제 사전 확인은 통과했지만 실제 예약 접수 요청을 확인하지 못했습니다. "
                    "열린 화면을 확인해주세요."
                )

            final_response = final_responses[-1]
            final_status = getattr(final_response, "status", None)
            self.log(
                f"[{worker_label}] [HTTP] 실제 예약 접수 · POST · "
                f"status={final_status if final_status is not None else '없음'} · "
                f"RTT {(time.perf_counter() - submit_started) * 1000:.0f}ms",
                "info" if final_status is not None and final_status < 400 else "warning",
            )
            if final_status is None or not 200 <= final_status < 400:
                return False, f"실제 예약 접수 응답 오류 · status={final_status or '없음'}"
            try:
                final_response_body = final_response.text()
            except Exception:
                final_response_body = ""

            deadline = time.monotonic() + 20.0
            body = ""
            while time.monotonic() < deadline:
                try:
                    body = page.locator("body").inner_text(timeout=2500)
                    if self._checkout_success(body, page.url, payment_url):
                        booking_number = self._checkout_number(
                            body,
                            order_code,
                            page.url,
                            final_response_body,
                        )
                        if booking_number:
                            return True, booking_number
                except Exception:
                    pass
                page.wait_for_timeout(250)
            return False, (
                "결제하기는 전송했지만 예약 완료 화면이나 예약번호를 확인하지 못했습니다. "
                "열린 화면을 확인해주세요."
            )
        except Exception as exc:
            return False, f"생성된 주문의 결제 처리 확인 필요: {self._describe_exception(exc)} · 열린 화면을 확인해주세요."

    def make_reservation_thread(self, reservation_data: dict[str, Any]) -> None:
        with self._worker_index_lock:
            worker_index = self._next_worker_index
            self._next_worker_index += 1
        worker_label = f"작업 {worker_index + 1}"
        branch = resolve_dpsnnn_branch(str(reservation_data.get("branch", "")), self.engine_options)
        alias = self._theme_alias(branch, reservation_data)
        date_str = str(reservation_data.get("reservationDate", ""))
        target_time = str(reservation_data.get("reservationTime", ""))[:5]
        session = create_dpsnnn_session()
        session.stop_event = self.stop_event
        reserve_url = urllib.parse.urljoin(branch["base_url"] + "/", branch["reserve_path"].lstrip("/"))
        prepared_payload: dict[str, str] | None = None
        prepared_slot_id = ""
        payload_prepared_at = 0.0
        prestage_retry_after = 0.0
        prestage_completed_slot_id = ""
        current_stage = "예약 홈 예열"
        try:
            while not self.stop_event.is_set():
                try:
                    started = time.perf_counter()
                    reserve_response = session.get(reserve_url, timeout=self.REQUEST_TIMEOUT)
                    reserve_response.raise_for_status()
                    break
                except requests.RequestException as exc:
                    self._log_http_diagnostic(worker_label, current_stage, "GET", None,
                                              time.perf_counter() - started, type(exc).__name__)
                    if self.stop_event.wait(0.5):
                        return
            else:
                return
            self._log_http_diagnostic(
                worker_label,
                current_stage,
                "GET",
                getattr(reserve_response, "status_code", None),
                time.perf_counter() - started,
                force=True,
            )
            reserve_response.raise_for_status()
            # Four sessions were the measured saturation point. Spreading their
            # request phases prevents all four from receiving the same pre-open
            # snapshot and then going idle together for a full round trip.
            initial_delay = worker_index * self.WORKER_STAGGER
            if initial_delay and self.stop_event.wait(initial_delay):
                return
            self.log(
                f"[{worker_label}] 단편선 슬롯 감시 시작 · 지점={branch['id']} · "
                f"날짜={date_str} · 시간={target_time}",
                "info",
            )
            last_message = ""
            while not self.stop_event.is_set():
                if self._order_claimed.is_set():
                    if self.submission_lock.acquire(blocking=False):
                        self.submission_lock.release()
                        break
                    self.stop_event.wait(0.01)
                    continue
                current_stage = "시간표 조회"
                try:
                    def poll_diagnostics(stage, method, status, elapsed, detail):
                        retry_detail = detail
                        if status is None or (status is not None and not 200 <= status < 300):
                            retry_detail = (
                                f"{detail} · {self.POLL_INTERVAL * 1000:.0f}ms 후 재시도"
                                if detail
                                else f"{self.POLL_INTERVAL * 1000:.0f}ms 후 재시도"
                            )
                        self._log_http_diagnostic(
                            worker_label,
                            stage,
                            method,
                            status,
                            elapsed,
                            retry_detail,
                            force=False,
                        )

                    warm = self._warm_checkout
                    if warm is not None and warm.ready.is_set() and warm.error:
                        self.log(warm.error, "error")
                        self.stop_event.set()
                        return
                    if warm is not None and warm.native_slot:
                        slots = [ZeroWorldTimeSlot(target_time, warm.native_slot, True)]
                    else:
                        slots = fetch_exact_dpsnnn_slots(
                            session, branch, alias, date_str, self.REQUEST_TIMEOUT,
                            diagnostics=poll_diagnostics)
                    matching = [item for item in slots if item.time == target_time and item.slot_id]
                    target = next((item for item in matching if item.available),
                                  matching[0] if matching else None)
                    if (
                        prepared_slot_id
                        and target is not None
                        and target.slot_id != prepared_slot_id
                    ):
                        prepared_payload = None
                        prepared_slot_id = ""
                        payload_prepared_at = 0.0
                        prestage_completed_slot_id = ""

                    if target is None or not target.available:
                        now_monotonic = time.monotonic()
                        if (
                            target is not None
                            and target.slot_id
                            and self._payload_prestage_due(date_str)
                            and now_monotonic >= prestage_retry_after
                            and target.slot_id != prestage_completed_slot_id
                            and not self._prepared_payload_usable(
                                prepared_payload,
                                target.slot_id,
                                payload_prepared_at,
                                date_str=date_str,
                                now_monotonic=now_monotonic,
                            )
                        ):
                            current_stage = "예약 주문 필드 사전 구성"
                            try:
                                candidate = self._build_order_payload(
                                    session,
                                    branch,
                                    target.slot_id,
                                    date_str,
                                    worker_label,
                                )
                                candidate_prepared_at = time.monotonic()
                                if not self._prepared_payload_usable(
                                    candidate,
                                    target.slot_id,
                                    candidate_prepared_at,
                                    date_str=date_str,
                                    now_monotonic=candidate_prepared_at,
                                ):
                                    raise ValueError(
                                        "사전 구성한 예약 주문 필드가 유효하지 않습니다."
                                    )
                                prepared_payload = candidate
                                prepared_slot_id = target.slot_id
                                payload_prepared_at = candidate_prepared_at
                                prestage_completed_slot_id = target.slot_id
                                self.log(
                                    f"[{worker_label}] [사전 준비] slotId={target.slot_id} · "
                                    "예약 상세 조회 2단계 완료",
                                    "info",
                                )
                            except (requests.RequestException, ValueError, KeyError):
                                prestage_retry_after = time.monotonic() + 0.25
                                raise

                        message = (
                            f"{target_time} 오픈 대기"
                            if target is not None
                            else f"{target_time} 시간표 게시 대기"
                        )
                        if message != last_message:
                            reason = (
                                "대상 슬롯 없음"
                                if target is None
                                else f"대상 슬롯 마감 · slotId={target.slot_id}"
                            )
                            self.silent_tick(
                                f"[{worker_label}] {message} · {reason} · "
                                f"{self.POLL_INTERVAL * 1000:.0f}ms 후 재조회"
                            )
                            last_message = message
                        self.stop_event.wait(self.POLL_INTERVAL)
                        continue

                    if warm is not None and not warm.ready.is_set():
                        self.stop_event.wait(0.025)
                        continue
                    if not self.submission_lock.acquire(blocking=False):
                        self.stop_event.wait(0.01)
                        continue

                    order_code = ""
                    message = ""
                    try:
                        if self._order_claimed.is_set() or self.stop_event.is_set():
                            break
                        if parse_bool_flag(reservation_data.get("devMode", False)):
                            self._order_claimed.set()
                            self.log(
                                f"[개발자 모드] 실제 슬롯 {target.slot_id} 확인 완료 · "
                                "주문 생성 없이 종료합니다.",
                                "success",
                            )
                            self.stop_event.set()
                            break

                        self.log(
                            f"[{worker_label}] [슬롯 확인] 시간={target_time} · "
                            f"slotId={target.slot_id} · 주문 생성 단계로 이동",
                            "info",
                        )
                        if self._prepared_payload_usable(
                            prepared_payload,
                            target.slot_id,
                            payload_prepared_at,
                            date_str=date_str,
                        ):
                            payload = prepared_payload
                            self.log(
                                f"[{worker_label}] [빠른 경로] slotId={target.slot_id} · "
                                "사전 구성한 주문 필드 사용",
                                "info",
                            )
                        else:
                            current_stage = "예약 주문 필드 구성"
                            payload = self._build_order_payload(
                                session,
                                branch,
                                target.slot_id,
                                date_str,
                                worker_label,
                            )
                        current_stage = "예약 주문 생성"
                        if self.stop_event.is_set() or (warm is not None and warm.error):
                            return
                        if self._journal is not None and not self._journal.claim():
                            self._order_claimed.set()
                            self.stop_event.set()
                            self.log("동일 예약의 기존 주문 기록 확인 · 중복 주문 차단", "warning")
                            return
                        # Claim BEFORE sending. A timeout cannot prove that the
                        # server did not accept this write.
                        self._order_claimed.set()
                        try:
                            order_code, message = self._add_order(
                                session, branch, payload, worker_label
                            )
                        except Exception as exc:
                            self._remember_order("unknown")
                            self.log(f"주문 응답 불명확 ({type(exc).__name__}) · 중복 방지를 위해 재전송을 중단했습니다. 예약 조회·알림톡을 확인해주세요.", "warning")
                            self.stop_event.set()
                            return
                        if order_code:
                            # This event is set while holding submission_lock. Any
                            # worker that already observed the same slot must pass
                            # the same lock and will exit before creating an order.
                            self._order_claimed.set()
                            self._remember_order("created", order_code)
                        else:
                            if not message or str(message).upper() == "SUCCESS":
                                self.stop_event.set()
                                self.log("주문 생성 여부 불명확 · 재주문 차단", "warning")
                                return
                            if self._journal is not None:
                                self._journal.rejected()
                            self._order_claimed.clear()
                            if warm is not None:
                                warm.native_slot = ""
                            prepared_payload = None
                            prepared_slot_id = ""
                            payload_prepared_at = 0.0
                            prestage_completed_slot_id = ""
                    finally:
                        self.submission_lock.release()

                    if not order_code:
                        retry_state = "주문 생성 응답에 orderId 없음"
                        if retry_state != last_message:
                            self.silent_tick(
                                f"[{worker_label}] [재시도] 예약 주문 생성 · {retry_state} · 120ms 후 재시도"
                            )
                            last_message = retry_state
                        self.stop_event.wait(0.12)
                        continue

                    self.log(
                        f"[{worker_label}] 결제 전 임시 주문 생성 완료 · orderId={order_code}",
                        "info",
                    )
                    current_stage = "결제 화면 완료"
                    completed, detail = self._complete_checkout(
                        session,
                        branch,
                        order_code,
                        reservation_data,
                        worker_label,
                    )
                    if completed:
                        booking_number = detail.strip()
                        self._remember_order("received", order_code, booking_number)
                        if booking_number:
                            self.log(
                                f"단편선 예약 접수 완료 · 입금 대기 · 예약번호 {booking_number}",
                                "success",
                            )
                        else:
                            self.log(
                                "단편선 예약 완료 · 사용자 조회용 예약번호는 "
                                "열린 완료 화면 또는 안내 문자에서 확인해주세요.",
                                "success",
                            )
                        self.log(
                            "[정보] 예약 완료 페이지를 열어둡니다. 확인 후 Chrome 창을 닫아주세요.",
                            "info",
                        )
                        self.notify_success(
                            BookingResult(True, "단편선 예약 접수 완료 · 알림톡 확인 후 입금해주세요.", booking_number)
                        )
                    else:
                        self._remember_order("checkout_unknown", order_code)
                        self.log(detail, "warning")
                        self.stop_event.set()
                    break
                except (requests.RequestException, ValueError, KeyError) as exc:
                    self._log_http_diagnostic(
                        worker_label,
                        current_stage,
                        "처리",
                        None,
                        0.0,
                        f"{self._describe_exception(exc)} · {self.POLL_INTERVAL * 1000:.0f}ms 후 재시도",
                    )
                    self.silent_tick(
                        f"[{worker_label}] {current_stage} 실패 · "
                        f"{type(exc).__name__} · {self.POLL_INTERVAL * 1000:.0f}ms 후 재시도"
                    )
                    self.stop_event.wait(self.POLL_INTERVAL)
        except Exception as exc:
            self.log(
                f"[{worker_label}] [오류] {current_stage} · {self._describe_exception(exc)}",
                "error",
            )
            raise
        finally:
            session.close()

    def stop_reservation(self) -> None:
        super().stop_reservation()
        if self._warm_checkout is not None:
            self._warm_checkout.close()

    async def make_reservation_async_task(self, reservation_data, task_idx) -> None:
        await asyncio.to_thread(self.make_reservation_thread, reservation_data)
