"""Coordination for DPSNNN reads and a thread-owned, prewarmed checkout.

No speculative booking URLs are used by the calendar observer. It selects the
actual enabled date and reads the URL reached by the site's own available card.
Playwright objects never cross threads; HTTP workers exchange plain values only.
"""
from __future__ import annotations

import queue
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from engines import browser_session

KST = timezone(timedelta(hours=9))


class ReadGovernor:
    def __init__(self):
        self.condition = threading.Condition()
        self.inflight = 0
        self.next_read = 0.0
        self.blocked_until = 0.0
        self.failures = 0
        self.priority_waiters = 0

    def acquire(self, priority=False, stop_event=None):
        with self.condition:
            if priority:
                self.priority_waiters += 1
            try:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        raise requests.RequestException("reservation stopped")
                    now = time.monotonic()
                    delay = max(self.blocked_until, 0 if priority else self.next_read) - now
                    if self.inflight < 4 and delay <= 0 and (priority or not self.priority_waiters):
                        self.inflight += 1
                        if not priority:
                            self.next_read = now + 0.05
                        return
                    self.condition.wait(max(0.01, min(0.1, delay)) if delay > 0 else 0.05)
            finally:
                if priority:
                    self.priority_waiters -= 1
                    self.condition.notify_all()

    def release(self, response=None, failed=False):
        status = getattr(response, "status_code", 0)
        with self.condition:
            self.inflight -= 1
            if failed or status == 429 or status >= 500:
                self.failures = min(self.failures + 1, 6)
                delay = min(8.0, 0.25 * 2 ** (self.failures - 1))
                retry = getattr(response, "headers", {}).get("Retry-After", "")
                try:
                    delay = max(delay, float(retry))
                except (TypeError, ValueError):
                    try:
                        delay = max(delay, parsedate_to_datetime(retry).timestamp() - time.time())
                    except (TypeError, ValueError, OverflowError):
                        pass
                self.blocked_until = max(self.blocked_until, time.monotonic() + delay)
            elif 200 <= status < 300:
                self.failures = 0
            self.condition.notify_all()


_governors = {}
_governor_lock = threading.Lock()


class DpsnnnSession(requests.Session):
    """Bound all Imweb traffic, including preparation, without replaying writes."""
    def request(self, method, url, **kwargs):
        parsed = urllib.parse.urlparse(url)
        with _governor_lock:
            governor = _governors.setdefault(parsed.netloc, ReadGovernor())
        priority = (parsed.path.endswith("/add_order.cm")
                    or parsed.path.endswith("/load_booking_detail_detail_calendar.cm")
                    or (parsed.path in {"/reserve_g", "/reserve_ss"}
                        and ("idx=" in parsed.query or "idx" in (kwargs.get("params") or {}))))
        governor.acquire(priority=priority,
                         stop_event=getattr(self, "stop_event", None))
        response = None
        try:
            response = super().request(method, url, **kwargs)
            return response
        finally:
            governor.release(response, failed=response is None)


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
                return False, "결제 응답 확인 중단 · 재주문하지 말고 열린 화면을 확인해주세요."
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
        chrome = None
        keep_open = False
        try:
            from playwright.sync_api import sync_playwright
            chrome = browser_session.start_isolated(log=self.log)
            if chrome is None:
                raise RuntimeError("독립 Chrome 슬롯을 확보하지 못했습니다. 기존 예약 창을 확인해주세요.")
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(chrome.endpoint)
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
                self.log("[사전 준비] 비회원 결제 브라우저 연결 완료", "info")
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
                    keep_open = True
                    try:
                        result.append(callback(context, page))
                    except Exception as exc:
                        result.append((False, f"결제 자동화 오류: {type(exc).__name__} · 열린 화면 확인 필요"))
                    finally:
                        done.set()
                    # Own the incognito context until its page is closed. Merely
                    # disconnecting its Playwright owner can dispose that context.
                    while browser.is_connected() and not page.is_closed():
                        page.wait_for_timeout(250)
                    break
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, RuntimeError) else f"결제 브라우저 준비 실패: {type(exc).__name__}"
            self.log(self.error, "error")
        finally:
            self.ready.set()
            self.finished.set()
            if chrome is not None:
                if keep_open:
                    def release_after_close():
                        while browser_session.cdp_descriptor(chrome.port):
                            time.sleep(0.5)
                        chrome.release()
                    threading.Thread(target=release_after_close, daemon=True).start()
                else:
                    chrome.close_if_launched()
                    chrome.release()
