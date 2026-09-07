"""Coordination for DPSNNN reads and a thread-owned, prewarmed checkout.

No speculative booking URLs are used by the calendar observer. It selects the
actual enabled date and reads the URL reached by the site's own available card.
Playwright objects never cross threads; HTTP workers exchange plain values only.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from engines import browser_session
from engines.dpsnnn_shared import SharedReadGovernor, ReservationCancelled

KST = timezone(timedelta(hours=9))


# Compatibility alias: there is now only one scheduler implementation.
ReadGovernor = SharedReadGovernor
LOGGER = logging.getLogger(__name__)
_governors = {}
_governor_lock = threading.Lock()


class DpsnnnSession(requests.Session):
    """Bound all Imweb traffic, including preparation, without replaying writes."""
    def request(self, method, url, **kwargs):
        parsed = urllib.parse.urlparse(url)
        key = parsed.netloc
        with _governor_lock:
            governor = _governors.get(key)
            if governor is None:
                governor = _governors[key] = SharedReadGovernor(parsed.netloc)
        priority = (parsed.path.endswith("/add_order.cm")
                    or parsed.path.endswith("/load_booking_detail_detail_calendar.cm")
                    or (parsed.path in {"/reserve_g", "/reserve_ss"}
                        and ("idx=" in parsed.query or "idx" in (kwargs.get("params") or {}))))
        begin = time.perf_counter()
        permit = None
        response = None
        sent = False
        acquired = begin
        http_end = begin
        try:
            permit = governor.acquire(priority=priority,
                                      stop_event=getattr(self, "stop_event", None))
            acquired = time.perf_counter()
            if getattr(self, "stop_event", None) is not None and self.stop_event.is_set():
                raise ReservationCancelled("reservation stopped")
            try:
                sent = True
                response = super().request(method, url, **kwargs)
                return response
            finally:
                http_end = time.perf_counter()
        finally:
            if permit is not None:
                try:
                    governor.release(response, failed=sent and response is None, permit=permit)
                except Exception as exc:
                    # A local cleanup error must never replace a received order
                    # response or the original network exception.
                    try:
                        governor.abandon(permit)
                    except Exception:
                        pass
                    LOGGER.warning("DPSNNN request cleanup failed: %s", type(exc).__name__)
            ended = time.perf_counter()
            if permit is None:
                acquired = ended
                http_end = ended
            self.last_timing = {
                "wait_ms": (acquired-begin)*1000,
                "http_ms": max(0, http_end-acquired)*1000,
                "total_ms": (ended-begin)*1000,
            }
            callback = getattr(self, "timing_callback", None)
            if callback is not None and not (
                    getattr(self, "stop_event", None) is not None and self.stop_event.is_set()):
                try:
                    callback(dict(self.last_timing), governor.snapshot(),
                             parsed.path.endswith("/html_list.cm"),
                             response is not None and 200 <= response.status_code < 300)
                except Exception:
                    pass


def wake_governors():
    with _governor_lock:
        governors = list(_governors.values())
    for governor in governors:
        governor.wake()



def detail_slot(url, base_url, reserve_path, date_str):
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if (parsed.scheme != "https" or parsed.netloc != urllib.parse.urlparse(base_url).netloc
            or parsed.path.rstrip("/") != reserve_path.rstrip("/")):
        return ""
    if query.get("day") != [date_str.replace("-", "")]:
        return ""
    if "endDay" in query and query["endDay"] != query["day"]:
        return ""
    idx = query.get("idx", [""])[0]
    return idx if re.fullmatch(r"\d+", idx) else ""


class WarmCheckout:
    def __init__(self, branch, reservation_data, log, stop_event):
        self.branch = branch
        self.data = dict(reservation_data)
        self.log = log
        self.stop_event = stop_event
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.error = ""
        self.jobs = queue.Queue()
        self.native_slot = ""
        self.native_seen_at = 0.0
        self._native_navigation_at = 0.0
        self._closing = threading.Event()
        self.thread = threading.Thread(target=self._run, name="DpsnnnCheckout", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self._closing.set()

    def submit(self, callback, timeout=180):
        if not self.ready.wait(35) or self.error or self.finished.is_set():
            return False, self.error or "결제 브라우저 준비 시간 초과"
        done = threading.Event()
        result = []
        self.jobs.put((callback, done, result))
        deadline = time.monotonic() + timeout
        while not done.wait(0.1):
            if self.finished.is_set() or time.monotonic() >= deadline:
                # The order may have been submitted. Never create a replacement.
                return False, "결제 응답 확인 중단 · 재주문하지 말고 예약 조회·알림톡을 확인해주세요."
        return result[0]

    def _observe_calendar(self, page):
        date_str = self.data["reservationDate"]
        if self._native_navigation_at:
            found = detail_slot(page.url, self.branch["base_url"], self.branch["reserve_path"], date_str)
            if found:
                self.native_slot = found
                self.native_seen_at = time.monotonic()
                self._native_navigation_at = 0.0
                self.log("[달력 확인] 목표 날짜의 예약 가능 카드와 실제 슬롯 확인", "info")
                return "detail"
            if time.monotonic() - self._native_navigation_at < 3:
                return "loading"
            self._native_navigation_at = 0.0
        target = datetime.strptime(date_str, "%Y-%m-%d")
        root = page.locator("booking-widget")
        if not root.count():
            return "loading"
        cell = root.locator(f'[role="gridcell"][data-day="{date_str}"]')
        if not cell.count():
            grid = root.locator('[role="grid"]')
            label = grid.get_attribute("aria-label") if grid.count() else ""
            match = re.search(r"(\d{4})년\s*(\d+)월", label or "")
            if match:
                shown = int(match[1]) * 12 + int(match[2])
                wanted = target.year * 12 + target.month
                if shown != wanted:
                    root.get_by_role("button", name="다음 달" if wanted > shown else "이전 달", exact=True).click(timeout=500)
            return "loading"
        button = cell.get_by_role("button")
        if not button.is_enabled():
            return "closed"
        if cell.get_attribute("aria-selected") != "true":
            button.click(timeout=500)
            return "loading"  # Let the site's dated product response render first.
        if not root.get_by_text(date_str.replace("-", "."), exact=True).count():
            return "loading"
        alias = str(self.data.get("engine_metadata", {}).get("theme", {}).get("alias")
                    or self.data["themePK"])
        label = f"{alias} / {str(self.data['reservationTime'])[:5]}"
        cards = root.locator('[class*="reservationItem_itemNameAnchor"]').filter(
            has=page.get_by_text(label, exact=True))
        for index in range(cards.count()):
            card = cards.nth(index)
            if not card.get_by_text("예약가능", exact=True).count():
                continue
            reserve = card.get_by_role("button", name="예약", exact=True)
            if not reserve.is_enabled():
                continue
            self._native_navigation_at = time.monotonic()
            reserve.click(timeout=400, no_wait_after=True)
            return "loading"
        return "soldout"

    def _run(self):
        browser = None
        visible_browser = None
        try:
            from playwright.sync_api import sync_playwright
            executable = browser_session.find_chrome()
            if executable is None:
                raise RuntimeError("Chrome 실행 파일을 찾지 못했습니다.")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
                # A separate cookie jar guarantees non-member checkout even if
                # the persistent Chrome profile has a previous member login.
                context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul")
                context.set_default_timeout(400)
                page = context.new_page()
                reserve_url = self.branch["base_url"] + self.branch["reserve_path"]
                open_at = (datetime.strptime(self.data["reservationDate"], "%Y-%m-%d")
                           - timedelta(days=6)).replace(tzinfo=KST).timestamp()
                last_navigation = 0.0
                calendar_state = "loading"
                self.ready.set()
                self.log("[사전 준비] 비회원 결제 브라우저 연결 완료 · 백그라운드", "info")
                while not self._closing.is_set():
                    try:
                        callback, done, result = self.jobs.get(timeout=0.05)
                    except queue.Empty:
                        if self.stop_event.is_set():
                            break
                        if self.native_slot:
                            continue
                        if calendar_state == "detail":
                            last_navigation = 0.0
                            calendar_state = "loading"
                        now = time.monotonic()
                        until_open = open_at - time.time()
                        interval = 30.0 if until_open > 5 else 1.0
                        try:
                            reload_due = (calendar_state in {"closed", "soldout"}
                                          and now - last_navigation >= interval)
                            if not last_navigation or reload_due or (
                                    calendar_state == "loading" and now - last_navigation >= 10):
                                last_navigation = now
                                calendar_state = "loading"
                                page.goto(reserve_url, wait_until="domcontentloaded", timeout=400)
                            calendar_state = self._observe_calendar(page)
                        except Exception:
                            pass  # HTTP observers remain independent of DOM changes.
                        continue
                    try:
                        result.append(callback(context, page))
                    except Exception as exc:
                        result.append((False, f"결제 자동화 오류: {type(exc).__name__} · 예약 조회·알림톡 확인 필요"))
                    finally:
                        done.set()
                    if result[0][0]:
                        # Display the confirmed receipt only; never replay writes.
                        try:
                            visible_browser = playwright.chromium.launch(
                                executable_path=str(executable), headless=False)
                            visible_context = visible_browser.new_context(
                                storage_state=context.storage_state(), locale="ko-KR", timezone_id="Asia/Seoul")
                            receipt = visible_context.new_page()
                            receipt.goto(page.url, wait_until="domcontentloaded", timeout=20000)
                            browser.close()
                            browser = None
                            while visible_browser.is_connected() and not receipt.is_closed():
                                receipt.wait_for_timeout(250)
                        except Exception as exc:
                            self.log(f"접수 완료 화면 표시 종료/실패 ({type(exc).__name__}) · 로그의 예약번호로 조회해주세요.", "warning")
                    else:
                        self.log("예약 결과 확인 필요 · 재주문하지 말고 예약 조회·알림톡을 확인해주세요.", "warning")
                    break
                if browser is not None:
                    browser.close()
                if visible_browser is not None and visible_browser.is_connected():
                    visible_browser.close()
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, RuntimeError) else f"결제 브라우저 준비 실패: {type(exc).__name__}"
            self.log(self.error, "error")
        finally:
            self.ready.set()
            self.finished.set()
