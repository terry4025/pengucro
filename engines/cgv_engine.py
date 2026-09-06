from __future__ import annotations

import io
import re
import threading
import time
import urllib.parse
import uuid
from typing import Any

from PIL import Image

from engines import browser_session
from engines.base_engine import BaseEngine
from engines.npay_keypad_recognizer import NpayKeypadRecognizer
from engines.cgv_client import (
    CGV_BFF_BOOKING_URL,
    CGV_BFF_CONTENT_URL,
    CGV_COMPANY_CODE,
    CGV_HOME_URL,
    CGV_MAX_WORKERS,
    CgvSeatGroup,
    is_contiguous_seat_group,
    is_physical_seat_group,
    normalize_seat_name,
    normalize_time,
    parse_seat_groups,
    parse_api_seats,
    schedule_items,
    select_schedule,
)
from pengucro.diagnostics import format_exception, redact_debug_text
from pengucro.models import BookingResult, parse_bool_flag


def _has_schedule_hint(payload: Any, movie: str, auditorium: str = "") -> bool:
    items = schedule_items(payload) if isinstance(payload, dict) else []
    canon_movie = re.sub(r"\s+", "", movie).casefold()
    if not canon_movie:
        return False
    for item in items:
        item_movie = re.sub(
            r"\s+", "", str(item.get("expoProdNm") or item.get("movNm") or "")
        ).casefold()
        if canon_movie and canon_movie in item_movie:
            return True
    return False


class CgvEngine(BaseEngine):
    """CGV watcher using a persistent, user-login Chrome session.

    Schedule reads are raced inside the real CGV page so the requests share the
    same cookies and browser network identity.  Only one browser controls the
    booking flow; duplicate account-level submissions would invalidate each
    other and are deliberately avoided.
    """

    MIN_POLL_INTERVAL = 0.12
    PREOPEN_IDLE_INTERVAL = 20.0
    SCHEDULE_HINT_INTERVAL = 2.0
    FAST_SEAT_LAUNCH_INTERVAL_MS = 120
    FAST_SEAT_MAX_INFLIGHT = CGV_MAX_WORKERS
    FAST_MONITOR_READ_INTERVAL = 0.025
    FAST_MONITOR_MAX_CONSECUTIVE_ERRORS = 5
    FAST_HOLD_TRANSACTION_TIMEOUT_MS = 8000
    FAST_MONITOR_RECONCILE_SECONDS = 1.0
    HEDGE_DELAY_MS = 110
    MAX_BACKOFF = 15.0
    BROWSER_SEAT_RELOAD_INTERVAL = 1.5
    RATE_LIMIT_BROWSER_RELOAD_INTERVAL = 4.0
    RATE_LIMIT_BROWSER_MAX_RELOAD_INTERVAL = 15.0
    VISITOR_SELECTION_TIMEOUT = 12.0
    VISITOR_RETRY_INTERVAL_MS = 350
    SCHEDULE_PROMOTION_ATTEMPTS = 3
    SCHEDULE_PROMOTION_RETRY_INTERVAL = 0.35
    SCHEDULE_PROMOTION_REWATCH_INTERVAL = 1.0
    API_UI_SYNC_ATTEMPTS = 40
    API_UI_SYNC_INTERVAL_MS = 25
    API_HOLD_UI_SYNC_MAX_ATTEMPTS = 1
    CAPTURED_REQUEST_HEADERS = ("authorization", "accept-language")
    SEAT_SUBMIT_TRANSITION_TIMEOUT_SECONDS = 10.0
    CGV_PAYMENT_PAGE_TIMEOUT_SECONDS = 15.0
    NPAY_PAGE_TIMEOUT_SECONDS = 20.0
    NPAY_CONTROL_TIMEOUT_SECONDS = 15.0
    NPAY_COMPLETION_TIMEOUT_SECONDS = 60.0
    PAYMENT_POLL_INTERVAL_MS = 100

    def __init__(self, log_callback, success_callback=None, **kwargs) -> None:
        super().__init__(log_callback, success_callback, **kwargs)
        self.scan_concurrency = 1
        self._browser_lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._chrome = None
        self._last_fast_monitor_exit_reason = ""
        self._initial_seat_response: dict[str, Any] | None = None
        self._last_fast_retry_after_seconds = 0.0
        self._developer_hold_cleanup: tuple[dict[str, Any], dict[str, Any]] | None = None
        self._api_hold_ui_schedule_key: tuple[str, ...] = ()

    def start_reservation(
        self, reservation_data: dict[str, Any], num_threads: int, is_async: bool = False
    ) -> None:
        self.scan_concurrency = max(1, min(int(num_threads), CGV_MAX_WORKERS))
        seat_concurrency = max(
            1,
            min(self.scan_concurrency, int(self.FAST_SEAT_MAX_INFLIGHT)),
        )
        self.log(
            f"CGV 회차 API 조회를 최대 {self.scan_concurrency}개 동시 연결로 시작합니다. "
            f"회차 감지 후 좌석 API는 최대 {seat_concurrency}개 연결, "
            f"{self.FAST_SEAT_LAUNCH_INTERVAL_MS}ms 간격으로 감시합니다. "
            "좌석 모달의 최초 응답을 우선 재사용하고 제한 신호가 감지되면 "
            "공식 브라우저 좌석 경로로 즉시 전환합니다.",
            "info",
        )
        super().start_reservation(reservation_data, 1, is_async=False)

    async def make_reservation_async_task(
        self, reservation_data: dict[str, Any], task_idx: int
    ) -> None:
        raise NotImplementedError

    @staticmethod
    def get_csrf_token(session: Any, url: str | None = None) -> str:
        return ""

    @staticmethod
    def _release_browser_lease_when_closed(chrome) -> None:
        def wait_for_close() -> None:
            while browser_session.cdp_descriptor(chrome.port):
                time.sleep(0.5)
            chrome.release()

        threading.Thread(
            target=wait_for_close,
            name=f"CgvChromeRelease-{chrome.port}",
            daemon=True,
        ).start()

    @staticmethod
    def _schedule_url(site_no: str, screening_date: str) -> str:
        query = urllib.parse.urlencode(
            {
                "coCd": CGV_COMPANY_CODE,
                "siteNo": site_no,
                "scnYmd": re.sub(r"\D", "", screening_date),
                "scnsNo": "",
                "scnSseq": "",
                "rtctlScopCd": "08",
                "custNo": "",
            }
        )
        return f"{CGV_BFF_BOOKING_URL}/searchMovScnInfo?{query}"

    @staticmethod
    def _cinema_url(site_no: str, site_name: str) -> str:
        return (
            f"{CGV_HOME_URL}/cnm/movieBook/cinema?"
            + urllib.parse.urlencode({"siteNo": site_no, "siteNm": site_name})
        )

    @staticmethod
    def _is_block_page(page) -> bool:
        try:
            text = page.locator("body").inner_text(timeout=2000)
        except Exception:
            return False
        normalized = re.sub(r"\s+", "", text)
        return "비정상적으로CGV에접속" in normalized or "이용이제한" in normalized

    @staticmethod
    def _is_recoverable_browser_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(
            err in msg
            for err in (
                "targetclosederror",
                "target closed",
                "page closed",
                "browser closed",
                "has been closed",
                "cdp disconnected",
                "connection closed",
                "session closed",
                "browser has been closed",
                "context or browser has been closed",
                "cannot navigate to invalid url",
                "net::err_connection_refused",
                "net::err_connection_reset",
            )
        )

    def _race_schedule(self, page, url: str, concurrency: int) -> dict[str, Any]:
        script = """
        async ({url, concurrency, hedgeDelayMs}) => {
          const started = performance.now();
          const controllers = Array.from({length: concurrency}, () => new AbortController());
          const requestHeaders = () => {
            const headers = new Headers({
              'Accept': 'application/json, text/plain, */*',
              'Accept-Language': 'ko-KR'
            });
            const item = String(document.cookie || '').split('; ')
              .find(value => value.startsWith('accessToken='));
            if (item) {
              const raw = item.slice('accessToken='.length);
              let token = raw;
              try { token = decodeURIComponent(raw); } catch (_) {}
              if (token) headers.set('Authorization', `Bearer ${token}`);
            }
            return headers;
          };
          return await new Promise((resolve) => {
            let failed = 0;
            const statuses = [];
            const timers = [];
            let settled = false;
            const launch = (controller, index) => {
              if (settled) return;
              fetch(url, {
                method: 'GET', cache: 'no-store', credentials: 'include',
                headers: requestHeaders(),
                signal: controller.signal
              }).then(async (response) => {
                statuses.push(response.status);
                if (!response.ok) throw new Error(String(response.status));
                const data = await response.json();
                settled = true;
                timers.forEach(clearTimeout);
                controllers.forEach((item, other) => { if (other !== index) item.abort(); });
                resolve({ok: true, status: response.status, data,
                         elapsedMs: performance.now() - started});
              }).catch((error) => {
                if (error && error.name === 'AbortError') return;
                failed += 1;
                if (failed === concurrency) {
                  settled = true;
                  resolve({ok: false, status: statuses[0] || 0, statuses,
                           error: String(error), elapsedMs: performance.now() - started});
                }
              });
            };
            controllers.forEach((controller, index) => {
              timers.push(setTimeout(() => launch(controller, index), index * hedgeDelayMs));
            });
          });
        }
        """
        result = page.evaluate(
            script,
            {
                "url": url,
                "concurrency": concurrency,
                "hedgeDelayMs": self.HEDGE_DELAY_MS,
            },
        )
        return dict(result) if isinstance(result, dict) else {"ok": False, "status": 0}

    @staticmethod
    def _seat_url(schedule: dict[str, Any], cust_no: str = "") -> str:
        query = urllib.parse.urlencode(
            {
                "coCd": schedule.get("coCd") or CGV_COMPANY_CODE,
                "siteNo": schedule.get("siteNo", ""),
                "scnYmd": schedule.get("scnYmd", ""),
                "scnsNo": schedule.get("scnsNo", ""),
                "scnSseq": schedule.get("scnSseq", ""),
                "custNo": cust_no,
            }
        )
        return f"{CGV_BFF_BOOKING_URL}/searchIfSeatData?{query}"

    def _begin_initial_seat_response_capture(self, page):
        """Capture the seat response already requested by CGV's own modal.

        Reusing this response avoids issuing a duplicate seat GET immediately
        after the official UI has finished loading the same data.  The request
        also provides the authorization headers added by CGV's current fetch
        wrapper, which a plain ``window.fetch`` call does not add by itself.
        """

        self._initial_seat_response = None

        def on_response(response) -> None:
            try:
                url = str(getattr(response, "url", "") or "")
                if "searchIfSeatData" not in url:
                    return
                status = int(getattr(response, "status", 0) or 0)
                request = getattr(response, "request", None)
                raw_headers: dict[str, Any] = {}
                if request is not None:
                    all_headers = getattr(request, "all_headers", None)
                    if callable(all_headers):
                        raw_headers = all_headers() or {}
                    else:
                        raw_headers = getattr(request, "headers", {}) or {}
                lowered = {
                    str(key).lower(): str(value)
                    for key, value in raw_headers.items()
                }
                request_headers = {
                    key: lowered[key]
                    for key in self.CAPTURED_REQUEST_HEADERS
                    if lowered.get(key)
                }
                payload = response.json() if 200 <= status < 300 else None
                self._initial_seat_response = {
                    "url": url,
                    "status": status,
                    "data": payload if isinstance(payload, dict) else None,
                    "requestHeaders": request_headers,
                }
            except Exception:
                # The normal DOM path remains available when CDP cannot read a
                # response body (for example, if the page was replaced).
                return

        try:
            page.on("response", on_response)
            return on_response
        except Exception:
            return None

    @staticmethod
    def _end_initial_seat_response_capture(page, handler) -> None:
        if handler is None:
            return
        for method_name in ("remove_listener", "off"):
            method = getattr(page, method_name, None)
            if not callable(method):
                continue
            try:
                method("response", handler)
                return
            except Exception:
                continue

    def _consume_initial_seat_response(
        self, schedule: dict[str, Any]
    ) -> dict[str, Any]:
        captured = self._initial_seat_response
        self._initial_seat_response = None
        if not isinstance(captured, dict):
            return {}
        if int(captured.get("status", 0) or 0) < 200 or int(
            captured.get("status", 0) or 0
        ) >= 300:
            return {}
        if not isinstance(captured.get("data"), dict):
            return {}

        try:
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(str(captured.get("url", ""))).query
            )
        except Exception:
            query = {}
        for key in ("siteNo", "scnYmd", "scnsNo", "scnSseq"):
            expected = str(schedule.get(key, "") or "")
            observed = str((query.get(key) or [""])[0] or "")
            if expected and observed and expected != observed:
                return {}
        return captured

    @staticmethod
    def _post_json(page, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = page.evaluate(
            """
            async ({url, payload}) => {
              try {
                const cookie = name => {
                  const prefix = `${name}=`;
                  const item = String(document.cookie || '').split('; ')
                    .find(value => value.startsWith(prefix));
                  if (!item) return '';
                  const raw = item.slice(prefix.length);
                  try { return decodeURIComponent(raw); } catch (_) { return raw; }
                };
                const headers = new Headers({
                  'Accept': 'application/json',
                  'Accept-Language': 'ko-KR',
                  'Content-Type': 'application/json',
                });
                const token = cookie('accessToken');
                if (token) headers.set('Authorization', `Bearer ${token}`);
                const response = await fetch(url, {
                  method: 'POST', credentials: 'include', cache: 'no-store',
                  headers,
                  body: JSON.stringify(payload)
                });
                const text = await response.text();
                let data = null;
                try { data = JSON.parse(text); } catch (_) {}
                return {ok: response.ok, status: response.status, data,
                        text: text.slice(0, 500)};
              } catch (error) {
                return {ok: false, status: 0, data: null, error: String(error)};
              }
            }
            """,
            {"url": url, "payload": payload},
        )
        return dict(result) if isinstance(result, dict) else {"ok": False, "status": 0}

    @staticmethod
    def _browser_auth_data(page) -> dict[str, str]:
        try:
            result = page.evaluate(
                """
                () => {
                  let query = {};
                  try { query = JSON.parse(sessionStorage.getItem('query') || '{}'); } catch (_) {}
                  let seatContext = null;
                  for (const node of document.querySelectorAll('body *')) {
                    const key = Object.keys(node).find(name => name.startsWith('__reactFiber$'));
                    let fiber = key ? node[key] : null;
                    for (let depth = 0; fiber && depth < 80; depth += 1, fiber = fiber.return) {
                      let dep = fiber.dependencies && fiber.dependencies.firstContext;
                      while (dep) {
                        const values = [dep.memoizedValue,
                          dep.context && dep.context._currentValue,
                          dep.context && dep.context._currentValue2];
                        for (const value of values) {
                          if (value && value.seatReload && value.setParams) seatContext = value;
                        }
                        dep = dep.next;
                      }
                    }
                    if (seatContext) break;
                  }
                  const cert = (seatContext && seatContext.nmbrCrtfData) || {};
                  const token = (seatContext && seatContext.accessTokenInfo) || {};
                  return {
                    custNo: String((seatContext && seatContext.custNo) || query.custNo || ''),
                    cusgdCd: String(token.cntCusgdCd || '01'),
                    bymd: String(cert.bymd || query.bymd || ''),
                    mbltNo: String(cert.mbltNo || query.mbltNo || ''),
                    nmbrCrtfNo: String(cert.encNmbrCrtfNo || cert.nmbrCrtfNo || query.nmbrCrtfNo || '')
                  };
                }
                """
            )
            return {str(key): str(value or "") for key, value in result.items()} if isinstance(result, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def choose_available_api_group(seats, groups: tuple[CgvSeatGroup, ...]):
        by_label = {seat.label: seat for seat in seats if seat.available and seat.sale_enabled}
        for group in groups:
            if is_contiguous_seat_group(group.seats) and all(
                label in by_label for label in group.seats
            ):
                chosen = tuple(by_label[label] for label in group.seats)
                if is_physical_seat_group(chosen, len(group.seats)):
                    return group, chosen
        return None

    @staticmethod
    def _sync_seat_payload_to_ui(page, payload: dict[str, Any]) -> bool:
        try:
            result = page.evaluate(
                """
                payload => {
                  const item = payload && payload.data && payload.data.items && payload.data.items[0];
                  if (!item) return false;
                  let store = null, seatContext = null;
                  for (const node of document.querySelectorAll('body *')) {
                    const key = Object.keys(node).find(name => name.startsWith('__reactFiber$'));
                    let fiber = key ? node[key] : null;
                    for (let depth = 0; fiber && depth < 80; depth += 1, fiber = fiber.return) {
                      let dep = fiber.dependencies && fiber.dependencies.firstContext;
                      while (dep) {
                        const values = [dep.memoizedValue,
                          dep.context && dep.context._currentValue,
                          dep.context && dep.context._currentValue2];
                        for (const value of values) {
                          if (value && value.store && value.store.dispatch) store = value.store;
                          if (value && value.seatReload && value.setSeatList) seatContext = value;
                        }
                        dep = dep.next;
                      }
                    }
                    if (store && seatContext) break;
                  }
                  if (!store) return false;
                  const fields = {
                    Sbord: item.sbord, SeatArea: item.seatArea, Seats: item.seats,
                    Salfrms: item.salfrms, Stknds: item.stknds, Szone: item.szone,
                    Szones: item.szones, Sblcks: item.sblcks
                  };
                  for (const [name, value] of Object.entries(fields)) {
                    if (value !== undefined) {
                      store.dispatch({type: `seatMap/reduxSet${name}`, payload: value});
                    }
                  }
                  if (seatContext) seatContext.setSeatList(item.seats || []);
                  return true;
                }
                """,
                payload,
            )
            return bool(result)
        except Exception:
            return False

    @staticmethod
    def _api_seat_selection_snapshot(page, seat_ids: list[str]) -> dict[str, Any]:
        try:
            result = page.evaluate(
                r"""
                seatIds => {
                  const clean = value => String(value || '').replace(/\s+/g, '');
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const selected = node => {
                    const classes = String(node.className || '').toLowerCase();
                    const tokens = new Set(classes.split(/[\s_\-]+/));
                    return node.title === '선택됨' ||
                           node.getAttribute('aria-pressed') === 'true' ||
                           node.getAttribute('aria-selected') === 'true' ||
                           tokens.has('selected') || tokens.has('active') || tokens.has('on');
                  };
                  const selectedIds = seatIds.filter(seatId =>
                    Array.from(document.querySelectorAll('button[data-seatlocno]'))
                      .filter(node => node.getAttribute('data-seatlocno') === seatId)
                      .some(selected)
                  );
                  const submit = Array.from(
                    document.querySelectorAll('button, a, div[role="button"]')
                  ).find(node => clean(node.textContent) === '선택완료' && visible(node));
                  const submitReady = Boolean(
                    submit && !submit.disabled && submit.getAttribute('aria-disabled') !== 'true'
                  );
                  return {
                    selectedIds,
                    submitPresent: Boolean(submit),
                    submitReady,
                    ready: selectedIds.length === seatIds.length && submitReady,
                  };
                }
                """,
                seat_ids,
            )
            return dict(result) if isinstance(result, dict) else {}
        except Exception:
            return {}

    def _wait_for_seat_selection_ready(self, page, seat_ids: list[str]) -> bool:
        observed_snapshot = False
        for _ in range(self.API_UI_SYNC_ATTEMPTS):
            snapshot = self._api_seat_selection_snapshot(page, seat_ids)
            if not snapshot:
                break
            observed_snapshot = True
            if snapshot.get("ready"):
                return True
            try:
                page.wait_for_timeout(self.API_UI_SYNC_INTERVAL_MS)
            except Exception:
                time.sleep(self.API_UI_SYNC_INTERVAL_MS / 1000.0)
        return not observed_snapshot

    def _select_api_seats_in_ui(self, page, payload, selected) -> bool:
        self._sync_seat_payload_to_ui(page, payload)
        seat_ids: list[str] = []
        for seat in selected:
            seat_id = getattr(seat, "seat_id", None) or (
                seat.get("seat_id") or seat.get("seatLocNo") or seat.get("id")
                if isinstance(seat, dict)
                else str(seat)
            )
            seat_id = str(seat_id or "")
            if not seat_id:
                return False
            seat_ids.append(seat_id)
            success = False
            for _ in range(5):
                if self._ensure_seat_selected_by_id(page, seat_id):
                    success = True
                    break
                try:
                    page.wait_for_timeout(50)
                except Exception:
                    time.sleep(0.05)
            if not success:
                return False

        # A DOM click only queues React's state update.  Wait until every seat
        # is visibly selected and the actual selection-complete button is
        # enabled; clicking it earlier (especially with force=True) is ignored.
        observed_snapshot = False
        for _ in range(self.API_UI_SYNC_ATTEMPTS):
            snapshot = self._api_seat_selection_snapshot(page, seat_ids)
            if not snapshot:
                break
            observed_snapshot = True
            if snapshot.get("ready"):
                return True
            selected_ids = {
                str(value) for value in snapshot.get("selectedIds", []) if value
            }
            for seat_id in seat_ids:
                if seat_id not in selected_ids:
                    self._ensure_seat_selected_by_id(page, seat_id)
            try:
                page.wait_for_timeout(self.API_UI_SYNC_INTERVAL_MS)
            except Exception:
                time.sleep(self.API_UI_SYNC_INTERVAL_MS / 1000.0)
        # Older test/mocked pages cannot expose a DOM snapshot.  The submit
        # helper still performs its own enabled-state check in that case.
        return not observed_snapshot

    @staticmethod
    def _ensure_seat_selected_by_id(page, seat_id: str) -> bool:
        """Ensure a seat is selected by its seat_id (loc no) idempotently.

        If the seat is already selected (via aria-pressed, aria-selected, or selected/active/on classes),
        returns True without clicking so the seat is not toggled off.
        If unselected and enabled, clicks it once to select.
        """
        seat_id = str(seat_id)
        try:
            result = page.evaluate(
                r"""
                (seatId) => {
                  const btn = document.querySelector(`button[data-seatlocno="${seatId}"]`);
                  if (!btn) return null;
                  const classes = String(btn.className || '').toLowerCase();
                  const tokens = new Set(classes.split(/[\s_\-]+/));
                  const isSelected = btn.getAttribute('aria-pressed') === 'true' ||
                                     btn.getAttribute('aria-selected') === 'true' ||
                                     btn.title === '선택됨' ||
                                     tokens.has('selected') || tokens.has('active') || tokens.has('on') ||
                                     (btn.classList && (btn.classList.contains('selected') || btn.classList.contains('active') || btn.classList.contains('on')));
                  if (isSelected) {
                    return true;
                  }
                  const unavailable = btn.disabled || btn.getAttribute('aria-disabled') === 'true' ||
                                      ['disabled', 'complete', 'sold', 'reserved', 'finish', 'soldout'].some(k => tokens.has(k) || classes.includes(k));
                  if (unavailable) {
                    return false;
                  }
                  if (typeof btn.scrollIntoView === 'function') {
                    btn.scrollIntoView({block: 'center', inline: 'center'});
                  }
                  btn.click();
                  return true;
                }
                """,
                seat_id,
            )
            if result is not None:
                return bool(result)
        except Exception:
            pass

        try:
            locator = page.locator(f'button[data-seatlocno="{seat_id}"]')
            for index in range(locator.count()):
                candidate = locator.nth(index)
                try:
                    if not (candidate.is_visible() and candidate.is_enabled()):
                        continue
                    if candidate.get_attribute("aria-disabled") == "true":
                        return False
                    aria_pressed = candidate.get_attribute("aria-pressed")
                    aria_selected = candidate.get_attribute("aria-selected")
                    classes = (candidate.get_attribute("class") or "").lower()
                    class_tokens = set(re.split(r"[\s_-]+", classes))
                    if (
                        aria_pressed == "true"
                        or aria_selected == "true"
                        or bool(class_tokens & {"selected", "active", "on"})
                    ):
                        return True
                    if any(
                        unavail in class_tokens or unavail in classes
                        for unavail in ("disabled", "complete", "sold", "reserved", "finish", "soldout")
                    ):
                        return False
                    candidate.click(timeout=1500)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    @staticmethod
    def _click_seat_by_id(page, seat_id: str) -> bool:
        return CgvEngine._ensure_seat_selected_by_id(page, seat_id)

    @staticmethod
    def _install_cached_hold_responses(page, price_response, hold_response) -> None:
        try:
            page.evaluate(
                """
                ({priceResponse, holdResponse}) => {
                  const original = window.__pengucroOriginalFetch || window.fetch.bind(window);
                  window.__pengucroOriginalFetch = original;
                  let priceUsed = false, holdUsed = false;
                  const make = value => new Response(JSON.stringify(value), {
                    status: 200, headers: {'Content-Type': 'application/json'}
                  });
                  window.fetch = async (...args) => {
                    const url = String(args[0] && args[0].url ? args[0].url : args[0]);
                    if (!priceUsed && url.includes('searchMovAtktSeatPrcList')) {
                      priceUsed = true; return make(priceResponse);
                    }
                    if (!holdUsed && url.includes('seatTempPrmp') && !url.includes('Cncl')) {
                      holdUsed = true;
                      setTimeout(() => { window.fetch = original; }, 1000);
                      return make(holdResponse);
                    }
                    return original(...args);
                  };
                }
                """,
                {"priceResponse": price_response, "holdResponse": hold_response},
            )
        except Exception:
            pass

    @staticmethod
    def _restore_fetch(page) -> None:
        try:
            page.evaluate(
                """() => { if (window.__pengucroOriginalFetch) {
                  window.fetch = window.__pengucroOriginalFetch;
                  delete window.__pengucroOriginalFetch;
                } }"""
            )
        except Exception:
            pass

    def _cancel_api_hold(self, page, hold_payload, hold_response) -> bool:
        data = hold_response.get("data", {}) if isinstance(hold_response, dict) else {}
        mov_atkt_no = str(data.get("movAtktNo", "")) if isinstance(data, dict) else ""
        if not mov_atkt_no:
            return False
        cancel_payload = {
            "coCd": hold_payload.get("coCd", CGV_COMPANY_CODE),
            "movAtktNo": mov_atkt_no,
            "sachlTypCd": hold_payload.get("sachlTypCd", "01"),
            "rtctlScopCd": hold_payload.get("rtctlScopCd", "08"),
            "custNo": hold_payload.get("custNo", ""),
            "seatPrmpDataList": hold_payload.get("seatPrmpDataList", []),
        }
        result = self._post_json(
            page, f"{CGV_BFF_CONTENT_URL}/seatTemp/seatTempPrmpCncl", cancel_payload
        )
        response_data = result.get("data") if isinstance(result, dict) else None
        api_status = (
            response_data.get("statusCode")
            if isinstance(response_data, dict)
            else None
        )
        api_success = api_status is None or str(api_status).strip() in {"", "0"}
        return bool(
            isinstance(result, dict)
            and result.get("ok")
            and api_success
        )

    @classmethod
    def _can_reuse_developer_cleanup_page(cls, page) -> bool:
        try:
            parsed = urllib.parse.urlparse(cls._safe_page_url(page))
            if (
                parsed.scheme.casefold() != "https"
                or (parsed.hostname or "").casefold() != "cgv.co.kr"
                or parsed.port not in {None, 443}
            ):
                return False
            is_closed = getattr(page, "is_closed", None)
            return not (callable(is_closed) and is_closed())
        except Exception:
            return False

    def _release_developer_api_hold(self, page) -> bool:
        cleanup = self._developer_hold_cleanup
        if cleanup is None:
            return False
        hold_payload, hold_response = cleanup
        cleanup_page = page if self._can_reuse_developer_cleanup_page(page) else None
        created_cleanup_pages = []

        def create_cleanup_page():
            context = getattr(self, "_context", None) or getattr(page, "context", None)
            if context is None:
                raise RuntimeError("CGV cleanup context unavailable")
            fresh_page = context.new_page()
            created_cleanup_pages.append(fresh_page)
            fresh_page.goto(
                CGV_HOME_URL,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            return fresh_page

        try:
            if cleanup_page is None:
                cleanup_page = create_cleanup_page()
            released = self._cancel_api_hold(cleanup_page, hold_payload, hold_response)
            if not released and cleanup_page is page:
                cleanup_page = create_cleanup_page()
                released = self._cancel_api_hold(
                    cleanup_page, hold_payload, hold_response
                )
            if not released:
                raise RuntimeError("CGV cleanup API rejected the request")
            self._developer_hold_cleanup = None
        except Exception as exc:
            self.log(
                "[개발자 모드] 임시선점 자동 해제 요청에 실패했습니다 "
                f"({format_exception(exc)}). 열린 결제 흐름은 그대로 두지 마세요.",
                "warning",
            )
            return False
        finally:
            for created_cleanup_page in created_cleanup_pages:
                try:
                    created_cleanup_page.close()
                except Exception:
                    pass
        self.log(
            "[개발자 모드] 검증에 사용한 CGV 임시선점 해제 요청을 완료했습니다.",
            "info",
        )
        return True

    @staticmethod
    def _direct_hold_config(
        schedule: dict[str, Any],
        people: int,
        auth: dict[str, str],
        cgv: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "priceUrl": f"{CGV_BFF_BOOKING_URL}/searchMovAtktSeatPrcList",
            "holdUrl": f"{CGV_BFF_CONTENT_URL}/seatTemp/seatTempPrmp",
            "schedule": {
                "coCd": str(schedule.get("coCd") or CGV_COMPANY_CODE),
                "siteNo": str(schedule.get("siteNo", "")),
                "scnsNo": str(schedule.get("scnsNo", "")),
                "scnYmd": str(schedule.get("scnYmd", "")),
                "scnSseq": str(schedule.get("scnSseq", "")),
                "movNo": str(schedule.get("movNo", "")),
                "rtctlScopCd": str(schedule.get("rtctlScopCd") or "08"),
                "prcrulDivCd": str(schedule.get("prcrulDivCd") or "01"),
                "sachlTypCd": str(schedule.get("sachlTypCd") or "01"),
            },
            "people": max(1, int(people)),
            "auth": {
                "custNo": str(auth.get("custNo", "")),
                "cusgdCd": str(auth.get("cusgdCd") or "01"),
                "bymd": re.sub(
                    r"\D",
                    "",
                    auth.get("bymd") or str(cgv.get("nonmember_birth", "")),
                ),
                "mbltNo": re.sub(
                    r"\D",
                    "",
                    auth.get("mbltNo") or str(cgv.get("nonmember_phone", "")),
                ),
                "nmbrCrtfNo": str(auth.get("nmbrCrtfNo", "")),
            },
        }

    def _start_fast_seat_monitor(
        self,
        page,
        seat_url: str,
        groups: tuple[CgvSeatGroup, ...],
        concurrency: int,
        *,
        launch_interval_ms: int | None = None,
        direct_hold: dict[str, Any] | None = None,
        request_headers: dict[str, str] | None = None,
        initial_payload: dict[str, Any] | None = None,
        max_conflicts: int = 0,
    ) -> bool:
        """Start a same-origin browser monitor with staggered persistent GETs."""

        interval = max(
            80,
            int(launch_interval_ms or self.FAST_SEAT_LAUNCH_INTERVAL_MS),
        )
        self._fast_monitor_attempt_id = uuid.uuid4().hex
        self._fast_monitor_requested_at = time.monotonic()
        try:
            result = page.evaluate(
                r"""
                ({url, groups, concurrency, intervalMs, maxConsecutiveErrors,
                  transactionTimeoutMs, directHold, requestHeaders, initialPayload,
                  maxConflicts, attemptId}) => {
                  const previous = window.__pengucroFastSeatMonitor;
                  if (previous && typeof previous.stop === 'function') previous.stop();

                  window.__pengucroCgvHoldReceipt = null;
                  const state = {
                    attemptId,
                    timing: {started: performance.now()},
                    running: true,
                    claiming: false,
                    phase: 'monitoring',
                    attempts: 0,
                    completed: 0,
                    inflight: 0,
                    consecutiveErrors: 0,
                    lastStatus: 0,
                    lastElapsedMs: 0,
                    blocked: false,
                    unauthorized: false,
                    failureKind: '',
                    retryAfterMs: 0,
                    lastError: '',
                    lastApiStatus: 0,
                    lastApiMessage: '',
                    lastFailureStage: '',
                    lastResultCode: '',
                    priceElapsedMs: 0,
                    holdElapsedMs: 0,
                    terminalError: '',
                    conflicts: 0,
                    hit: null,
                    initialPayloadUsed: false,
                    timer: null,
                    controllers: new Set(),
                  };
                  const recordApiFailure = (stage, payload, fallbackStatus = -1) => {
                    const status = Number(payload && payload.statusCode != null
                      ? payload.statusCode : fallbackStatus);
                    const detail = payload && payload.data || {};
                    const message = [payload && payload.statusMessage, detail.resultMessage, detail.resultMsg]
                      .filter(Boolean).join(' · ');
                    state.lastFailureStage = stage;
                    state.lastResultCode = String(detail.resultCode ?? '');
                    state.lastApiStatus = status;
                    state.lastApiMessage = message;
                    state.lastError = `${stage} API ${status}${message ? `: ${message}` : ''}`;
                  };
                  const isSeatConflict = payload => {
                    const detail = payload && payload.data || {};
                    const message = [payload && payload.statusMessage, detail.resultMessage, detail.resultMsg]
                      .filter(Boolean).join(' · ');
                    return /좌석|seat/i.test(message) &&
                      /이미|매진|판매완료|선점된|선점되어|다른.*(?:고객|사용자)|occupied|sold.out|not.available/i.test(message);
                  };
                  const normalize = value => String(value || '')
                    .toUpperCase().replace(/[\s_-]+/g, '');
                  const normalizedGroups = groups.map(group => group.map(normalize));
                  const cookieValue = name => {
                    const prefix = `${name}=`;
                    const item = String(document.cookie || '').split('; ')
                      .find(value => value.startsWith(prefix));
                    if (!item) return '';
                    const raw = item.slice(prefix.length);
                    try { return decodeURIComponent(raw); } catch (_) { return raw; }
                  };
                  const buildHeaders = extra => {
                    const headers = new Headers(requestHeaders || {});
                    const token = cookieValue('accessToken');
                    if (!headers.has('Authorization') && token) {
                      headers.set('Authorization', `Bearer ${token}`);
                    }
                    headers.set('Accept-Language', 'ko-KR');
                    for (const [name, value] of Object.entries(extra || {})) {
                      headers.set(name, value);
                    }
                    return headers;
                  };
                  const retryAfterMs = response => {
                    const value = response && response.headers
                      ? response.headers.get('Retry-After') : '';
                    if (!value) return 0;
                    const seconds = Number(value);
                    if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
                    const date = Date.parse(value);
                    return Number.isFinite(date) ? Math.max(0, date - Date.now()) : 0;
                  };
                  const markHttpFailure = (response, stage = 'seat') => {
                    state.lastStatus = response.status;
                    state.retryAfterMs = retryAfterMs(response);
                    if (response.status === 401) {
                      state.unauthorized = true;
                      state.failureKind = 'unauthorized';
                    } else if (response.status === 403) {
                      state.blocked = true;
                      state.failureKind = 'forbidden';
                    } else if (response.status === 429) {
                      state.blocked = true;
                      state.failureKind = 'rate-limited';
                    }
                    state.lastError = `${stage} HTTP ${response.status}`;
                  };
                  const availableSeats = payload => {
                    const data = payload && payload.data ? payload.data : payload;
                    let items = data && data.items ? data.items : [];
                    if (!Array.isArray(items)) items = items ? [items] : [];
                    const seatsByLabel = new Map();
                    for (const item of items) {
                      const seats = item && Array.isArray(item.seats) ? item.seats : [];
                      for (const seat of seats) {
                        if (String(seat.seatStusCd || '') !== '00') continue;
                        if (String(seat.seatSaleYn || 'Y').toUpperCase() !== 'Y') continue;
                        const row = String(seat.seatRowNm || '').toUpperCase();
                        const no = String(seat.seatNo || '');
                        const num = parseInt(no, 10);
                        const label = normalize(`${row}${no}`);
                        seatsByLabel.set(label, seat);
                        if (!isNaN(num)) {
                          seatsByLabel.set(normalize(`${row}${num}`), seat);
                          seatsByLabel.set(normalize(`${row}${String(num).padStart(2, '0')}`), seat);
                        }
                      }
                    }
                    return seatsByLabel;
                  };
                  const findGroup = payload => {
                    const available = availableSeats(payload);
                    for (const group of normalizedGroups) {
                      if (group.every(label => available.has(label))) {
                        return {labels: group, seats: group.map(label => available.get(label))};
                      }
                    }
                    return null;
                  };
                  state.stop = () => {
                    state.running = false;
                    state.claiming = false;
                    state.phase = 'stopped';
                    if (state.timer) clearInterval(state.timer);
                    state.timer = null;
                    for (const controller of state.controllers) controller.abort();
                    state.controllers.clear();
                  };
                  const pauseOtherRequests = keep => {
                    state.claiming = true;
                    state.phase = 'claiming';
                    if (state.timer) clearInterval(state.timer);
                    state.timer = null;
                    for (const controller of state.controllers) {
                      if (controller !== keep) controller.abort();
                    }
                  };
                  const postJson = async (targetUrl, body, signal) => {
                    const response = await fetch(targetUrl, {
                      method: 'POST',
                      credentials: 'include',
                      cache: 'no-store',
                      headers: buildHeaders({
                        'Accept': 'application/json',
                        'Content-Type': 'application/json',
                      }),
                      body: JSON.stringify(body),
                      signal,
                    });
                    let data = null;
                    try { data = await response.json(); } catch (_) {}
                    return {
                      ok: response.ok,
                      status: response.status,
                      data,
                      retryAfterMs: retryAfterMs(response),
                    };
                  };
                  const buildPricePayload = selected => {
                    const schedule = directHold.schedule;
                    return {
                      coCd: schedule.coCd,
                      siteNo: schedule.siteNo,
                      scnsNo: schedule.scnsNo,
                      scnYmd: schedule.scnYmd,
                      scnSseq: schedule.scnSseq,
                      movNo: schedule.movNo,
                      rtctlScopCd: schedule.rtctlScopCd,
                      prcrulDivCd: schedule.prcrulDivCd,
                      sachlTypCd: schedule.sachlTypCd,
                      prodBnduList: [{prodBnduCd: '01', prodBnduQty: directHold.people}],
                      seatList: selected.map(seat => ({
                        seatLocNo: String(seat.seatLocNo || ''),
                        szoneKindCd: String(seat.szoneKindCd || ''),
                        stkndCd: String(seat.stkndCd || ''),
                        seatSalfrmCd: String(seat.seatSalfrmCd || ''),
                        prodBnduCd: '01',
                      })),
                      zoneGroupYn: selected.some(
                        seat => String(seat.szoneSalpolCd || '') === '02'
                      ) ? 'Y' : 'N',
                    };
                  };
                  const buildHoldPayload = selected => {
                    const schedule = directHold.schedule;
                    const auth = directHold.auth;
                    return {
                      coCd: schedule.coCd,
                      bymd: auth.bymd,
                      mbltNo: auth.mbltNo,
                      siteNo: schedule.siteNo,
                      scnYmd: schedule.scnYmd,
                      scnsNo: schedule.scnsNo,
                      scnSseq: schedule.scnSseq,
                      movAtktNo: '',
                      custNo: auth.custNo,
                      cusgdCd: auth.cusgdCd,
                      nmbrCrtfNo: auth.nmbrCrtfNo,
                      sachlCd: '10',
                      atktChnlCd: '01',
                      sachlTypCd: schedule.sachlTypCd,
                      rtctlScopCd: schedule.rtctlScopCd,
                      seatPrmpDataList: selected.map(seat => ({
                        seatRowNm: String(seat.seatRowNm || ''),
                        seatNo: String(seat.seatNo || ''),
                        seatLocNo: String(seat.seatLocNo || ''),
                        sbordNo: String(seat.sbordNo || ''),
                        seatAreaNo: String(seat.seatAreaNo || ''),
                        szoneNo: String(seat.szoneNo || ''),
                      })),
                    };
                  };
                  let queuedPayload = initialPayload && typeof initialPayload === 'object'
                    ? initialPayload : null;
                  let launch;
                  const resume = () => {
                    if (state.hit || state.blocked || state.unauthorized || state.terminalError) return;
                    state.claiming = false;
                    state.phase = 'monitoring';
                    state.running = true;
                    if (state.timer) clearInterval(state.timer);
                    state.timer = setInterval(launch, intervalMs);
                    setTimeout(launch, 0);
                  };
                  const conflictOrResume = () => {
                    state.conflicts += 1;
                    if (maxConflicts > 0 && state.conflicts >= maxConflicts) {
                      state.failureKind = 'seat-conflict';
                      state.running = false;
                      state.claiming = false;
                      state.phase = 'conflicted';
                      if (state.timer) clearInterval(state.timer);
                      state.timer = null;
                      return;
                    }
                    resume();
                  };
                  launch = async () => {
                    if (!state.running || state.claiming || state.hit ||
                        state.blocked || state.unauthorized || state.terminalError ||
                        state.inflight >= concurrency) return;
                    const controller = new AbortController();
                    state.controllers.add(controller);
                    state.inflight += 1;
                    state.attempts += 1;
                    const started = performance.now();
                    try {
                      let payload = null;
                      if (queuedPayload) {
                        payload = queuedPayload;
                        queuedPayload = null;
                        state.initialPayloadUsed = true;
                        state.lastStatus = 200;
                        state.lastElapsedMs = 0;
                      } else {
                        const response = await fetch(url, {
                          method: 'GET',
                          cache: 'no-store',
                          credentials: 'include',
                          headers: buildHeaders({
                            'Accept': 'application/json, text/plain, */*',
                          }),
                          signal: controller.signal,
                        });
                        state.lastStatus = response.status;
                        state.lastElapsedMs = performance.now() - started;
                        if ([401, 403, 429].includes(response.status)) {
                          markHttpFailure(response);
                          state.stop();
                          return;
                        }
                        if (!response.ok) throw new Error(`HTTP ${response.status}`);
                        payload = await response.json();
                      }
                      const apiStatus = Number(payload && payload.statusCode != null
                        ? payload.statusCode : 0);
                      if (apiStatus !== 0) {
                        recordApiFailure('seat', payload, apiStatus);
                        if (apiStatus === -1001 || apiStatus === -1002) {
                          state.unauthorized = true;
                          state.failureKind = 'unauthorized';
                          state.stop();
                          return;
                        }
                        throw new Error(state.lastError);
                      }
                      state.consecutiveErrors = 0;
                      state.timing.seatReady = performance.now();
                      const group = findGroup(payload);
                      state.timing.candidateReady = performance.now();
                      if (group) {
                        pauseOtherRequests(controller);
                        if (!directHold) {
                          state.claiming = false;
                          state.phase = 'matched';
                          state.hit = {
                            data: payload,
                            group: group.labels,
                            elapsedMs: state.lastElapsedMs,
                          };
                          return;
                        }

                        const transactionStarted = performance.now();
                        const transactionController = new AbortController();
                        state.controllers.add(transactionController);
                        const transactionTimer = setTimeout(
                          () => transactionController.abort(),
                          transactionTimeoutMs,
                        );
                        let holdSent = false;
                        try {
                          state.phase = 'pricing';
                          state.timing.priceStarted = performance.now();
                          delete state.timing.priceFinished;
                          delete state.timing.holdStarted;
                          delete state.timing.holdFinished;
                          state.priceElapsedMs = 0;
                          state.holdElapsedMs = 0;
                          const pricePayload = buildPricePayload(group.seats);
                          const price = await postJson(
                            directHold.priceUrl,
                            pricePayload,
                            transactionController.signal,
                          );
                          state.timing.priceFinished = performance.now();
                          state.priceElapsedMs = state.timing.priceFinished - state.timing.priceStarted;
                          state.lastStatus = price.status;
                          if (price.status === 401) {
                            state.unauthorized = true;
                            state.failureKind = 'unauthorized';
                            state.lastError = `price HTTP ${price.status}`;
                            state.stop();
                            return;
                          }
                          if (price.status === 403 || price.status === 429) {
                            state.blocked = true;
                            state.failureKind = price.status === 403
                              ? 'forbidden' : 'rate-limited';
                            state.retryAfterMs = Number(price.retryAfterMs || 0);
                            state.lastError = `price HTTP ${price.status}`;
                            state.stop();
                            return;
                          }
                          if (!price.data || typeof price.data !== 'object') {
                            state.terminalError = 'price-response-shape';
                            state.lastError = 'price response is not JSON';
                            state.stop();
                            return;
                          }
                          const priceApiStatus = Number(price.data.statusCode ?? -1);
                          if (priceApiStatus === -1001 || priceApiStatus === -1002) {
                            recordApiFailure('price', price.data, priceApiStatus);
                            state.unauthorized = true;
                            state.failureKind = 'unauthorized';
                            state.stop();
                            return;
                          }
                          if (!price.ok || priceApiStatus !== 0) {
                            recordApiFailure('price', price.data);
                            if (isSeatConflict(price.data)) conflictOrResume();
                            else { state.terminalError = 'price-rejected'; state.stop(); }
                            return;
                          }

                          state.phase = 'holding';
                          const holdPayload = buildHoldPayload(group.seats);
                          holdSent = true;
                          const holdStarted = performance.now();
                          state.timing.holdStarted = holdStarted;
                          const hold = await postJson(
                            directHold.holdUrl,
                            holdPayload,
                            transactionController.signal,
                          );
                          state.timing.holdFinished = performance.now();
                          state.holdElapsedMs = state.timing.holdFinished - holdStarted;
                          state.lastStatus = hold.status;
                          if (hold.status === 401) {
                            state.unauthorized = true;
                            state.failureKind = 'unauthorized';
                            state.lastError = `hold HTTP ${hold.status}`;
                            state.stop();
                            return;
                          }
                          if (hold.status === 403 || hold.status === 429) {
                            state.blocked = true;
                            state.failureKind = hold.status === 403
                              ? 'forbidden' : 'rate-limited';
                            state.retryAfterMs = Number(hold.retryAfterMs || 0);
                            state.lastError = `hold HTTP ${hold.status}`;
                            state.stop();
                            return;
                          }
                          if (!hold.data || typeof hold.data !== 'object') {
                            state.terminalError = 'hold-uncertain';
                            state.lastError = 'hold response is not JSON';
                            state.stop();
                            return;
                          }
                          const holdApiStatus = Number(hold.data.statusCode ?? -1);
                          if (holdApiStatus === -1001 || holdApiStatus === -1002) {
                            recordApiFailure('hold', hold.data, holdApiStatus);
                            state.unauthorized = true;
                            state.failureKind = 'unauthorized';
                            state.stop();
                            return;
                          }
                          const holdData = hold.data.data || {};
                          const held = hold.ok
                            && holdApiStatus === 0
                            && String(holdData.resultCode ?? '0') === '0'
                            && Boolean(holdData.movAtktNo);
                          if (!held) {
                            recordApiFailure('hold', hold.data);
                            if (holdData.movAtktNo || hold.status >= 500 ||
                                (holdApiStatus === 0 && String(holdData.resultCode ?? '0') === '0')) {
                              state.terminalError = 'hold-uncertain'; state.stop();
                            } else if (isSeatConflict(hold.data)) conflictOrResume();
                            else { state.terminalError = 'hold-rejected'; state.stop(); }
                            return;
                          }
                          state.claiming = false;
                          state.phase = 'held';
                          state.hit = {
                            data: payload,
                            group: group.labels,
                            elapsedMs: state.lastElapsedMs,
                            transaction: {
                              priceResponse: price.data,
                              holdResponse: hold.data,
                              holdPayload,
                              elapsedMs: performance.now() - transactionStarted,
                              timing: {...state.timing},
                            },
                          };
                          window.__pengucroCgvHoldReceipt = {
                            attemptId, hit: state.hit, confirmedAt: performance.now(),
                          };
                        } catch (error) {
                          if (holdSent) {
                            state.lastFailureStage = 'hold';
                            state.lastError = 'hold response lost; outcome unknown';
                            state.terminalError = 'hold-uncertain';
                            state.stop();
                          } else {
                            state.lastFailureStage = 'price';
                            state.lastError = 'price request failed before hold';
                            state.terminalError = 'price-transport-error';
                            state.stop();
                          }
                        } finally {
                          clearTimeout(transactionTimer);
                          state.controllers.delete(transactionController);
                          if (state.hit || state.blocked || state.unauthorized || state.terminalError) {
                            state.claiming = false;
                          }
                        }
                      }
                    } catch (error) {
                      if (!(error && error.name === 'AbortError')) {
                        state.consecutiveErrors += 1;
                        state.lastError = String(error || 'fetch failed');
                        if (state.consecutiveErrors >= maxConsecutiveErrors) state.stop();
                      }
                    } finally {
                      state.controllers.delete(controller);
                      state.inflight = Math.max(0, state.inflight - 1);
                      state.completed += 1;
                    }
                  };

                  window.__pengucroFastSeatMonitor = state;
                  state.timer = setInterval(launch, intervalMs);
                  launch();
                  return true;
                }
                """,
                {
                    "url": seat_url,
                    "groups": [list(group.seats) for group in groups],
                    "concurrency": max(1, min(int(concurrency), CGV_MAX_WORKERS)),
                    "intervalMs": interval,
                    "maxConsecutiveErrors": self.FAST_MONITOR_MAX_CONSECUTIVE_ERRORS,
                    "transactionTimeoutMs": self.FAST_HOLD_TRANSACTION_TIMEOUT_MS,
                    "directHold": direct_hold,
                    "requestHeaders": dict(request_headers or {}),
                    "initialPayload": initial_payload,
                    "maxConflicts": max(0, int(max_conflicts)),
                    "attemptId": self._fast_monitor_attempt_id,
                },
            )
            return bool(result)
        except Exception:
            return False
        finally:
            self._fast_monitor_ack_at = time.monotonic()

    @staticmethod
    def _read_fast_seat_monitor(page) -> dict[str, Any]:
        try:
            result = page.evaluate(
                r"""
                () => {
                  const state = window.__pengucroFastSeatMonitor;
                  if (!state) return null;
                  return {
                    attemptId: state.attemptId,
                    timing: state.timing,
                    running: state.running,
                    claiming: state.claiming,
                    phase: state.phase,
                    attempts: state.attempts,
                    completed: state.completed,
                    inflight: state.inflight,
                    consecutiveErrors: state.consecutiveErrors,
                    lastStatus: state.lastStatus,
                    lastElapsedMs: state.lastElapsedMs,
                    blocked: state.blocked,
                    unauthorized: state.unauthorized,
                    failureKind: state.failureKind,
                    retryAfterMs: state.retryAfterMs,
                    lastError: state.lastError,
                    lastApiStatus: state.lastApiStatus,
                    lastApiMessage: state.lastApiMessage,
                    lastFailureStage: state.lastFailureStage,
                    lastResultCode: state.lastResultCode,
                    priceElapsedMs: state.priceElapsedMs,
                    holdElapsedMs: state.holdElapsedMs,
                    terminalError: state.terminalError,
                    conflicts: state.conflicts,
                    initialPayloadUsed: state.initialPayloadUsed,
                    hit: state.hit,
                  };
                }
                """
            )
            return dict(result) if isinstance(result, dict) else {}
        except Exception:
            return {}

    def _recover_fast_monitor_snapshot(self, page, schedule, groups) -> dict[str, Any]:
        """Read this invocation's browser state/confirmed receipt; never submit.

        This handles CDP reply loss. A lost server response without a verified
        receipt remains unknown: no unverified reservation-list API is called.
        """
        attempt_id = getattr(self, "_fast_monitor_attempt_id", "")
        if not attempt_id:
            return {}
        deadline = time.monotonic() + self.FAST_MONITOR_RECONCILE_SECONDS
        while not self.stop_event.is_set():
            try:
                receipt = page.evaluate(r"""id => {
                  const receipt = window.__pengucroCgvHoldReceipt;
                  if (!receipt || receipt.attemptId !== id ||
                      performance.now() - receipt.confirmedAt > 15000) return null;
                  return receipt.hit;
                }""", attempt_id)
                if isinstance(receipt, dict):
                    transaction = receipt.get("transaction") or {}
                    payload = transaction.get("holdPayload") or {}
                    response = transaction.get("holdResponse") or {}
                    data = response.get("data") or {}
                    price = transaction.get("priceResponse") or {}
                    identity = ("siteNo", "scnYmd", "scnsNo", "scnSseq")
                    expected_groups = {frozenset(normalize_seat_name(s) for s in g.seats) for g in groups}
                    actual_group = frozenset(normalize_seat_name(s) for s in receipt.get("group", []))
                    if (all(str(payload.get(k, "")) == str(schedule.get(k, "")) for k in identity)
                            and all(payload.get(k) for k in identity)
                            and actual_group in expected_groups
                            and price.get("statusCode") == 0
                            and response.get("statusCode") == 0
                            and str(data.get("resultCode", "0")) == "0" and data.get("movAtktNo")):
                        self.log("[CGV] 동일 선점 요청의 브라우저 성공 응답 복원 · 추가 제출 없이 결제 연결", "success")
                        return {"hit": receipt, "timing": transaction.get("timing", {})}
                snapshot = self._read_fast_seat_monitor(page)
                if snapshot.get("attemptId") == attempt_id:
                    return snapshot
            except Exception:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.stop_event.wait(min(0.05, remaining)):
                return {}
        return {}

    def _log_fast_monitor_timing(self, snapshot) -> None:
        timing = snapshot.get("timing") or {}
        requested = getattr(self, "_fast_monitor_requested_at", None)
        acknowledged = getattr(self, "_fast_monitor_ack_at", None)
        opened = getattr(self, "_cgv_open_detected_at", None)
        bridge = ""
        if requested is not None and acknowledged is not None:
            bridge += f" · 감시 설치 왕복 {(acknowledged-requested)*1000:.0f}ms"
        if requested is not None and opened is not None:
            bridge += f" · 회차 감지→감시 설치 요청 {(requested-opened)*1000:.0f}ms"
        def elapsed(first, last):
            a, b = timing.get(first), timing.get(last)
            return f"{max(0, float(b)-float(a)):.0f}ms" if a is not None and b is not None else "미확인"
        self.log("[CGV 속도] 브라우저 처리 · 좌석 확인→후보 " + elapsed("seatReady", "candidateReady")
                 + " · 후보→가격 발송 " + elapsed("candidateReady", "priceStarted")
                 + " · 가격 왕복 " + elapsed("priceStarted", "priceFinished")
                 + " · 가격 완료→선점 발송 " + elapsed("priceFinished", "holdStarted")
                 + " · 선점 왕복 " + elapsed("holdStarted", "holdFinished")
                 + " · 감시 설치→선점 발송 " + elapsed("started", "holdStarted") + bridge, "info")

    @staticmethod
    def _stop_fast_seat_monitor(page) -> None:
        try:
            page.evaluate(
                r"""
                () => {
                  const state = window.__pengucroFastSeatMonitor;
                  if (state && typeof state.stop === 'function') state.stop();
                  delete window.__pengucroFastSeatMonitor;
                }
                """
            )
        except Exception:
            pass

    def _fast_monitor_conflict_limit(self) -> int:
        """Return how many direct-hold conflicts end one monitor invocation.

        The base single-screen monitor keeps retrying forever. Priority-ladder
        runtimes temporarily override this hook so a lost race can immediately
        continue with the next seat group or screening instead of becoming
        pinned to the first preflight winner.
        """

        return 0

    def _prepare_api_hold_ui(
        self,
        page,
        schedule: dict[str, Any],
        people: int,
    ) -> bool:
        """Prepare the browser UI after an API hold and before seat sync.

        The opening race starts from the authenticated CGV page, so the direct
        seat read/hold can finish before the slower visitor and seat-modal UI
        transition.  Once the server hold is ours, prepare that UI while keeping
        the hold alive, then let the existing synchronization path take over.
        """

        schedule_key = tuple(
            str(schedule.get(key, "") or "")
            for key in ("siteNo", "scnYmd", "scnsNo", "scnSseq")
        )
        if schedule_key and schedule_key == self._api_hold_ui_schedule_key:
            return True
        self.log(
            "[CGV] 좌석 API 임시선점 완료 후 관람인원·좌석 화면을 동기화합니다.",
            "info",
        )
        try:
            return self._prepare_published_schedule(page, schedule, people)
        finally:
            # The hold transaction already owns the authoritative seat payload.
            # Do not let the modal's later capture seed a fallback with stale data.
            self._initial_seat_response = None

    def _sync_held_seats_for_checkout(self, page, payload, selected) -> bool:
        """Mirror an already-held seat group without releasing it on first lag.

        The direct API transaction has already won the actual seat race.  A
        temporary React/DOM mismatch must therefore be retried locally while
        the server hold remains intact.  The base engine keeps one attempt for
        backward compatibility; the final runtime raises the bounded limit.
        """

        attempts = max(1, int(self.API_HOLD_UI_SYNC_MAX_ATTEMPTS))
        for attempt in range(attempts):
            if self.stop_event.is_set():
                return False
            if self._select_api_seats_in_ui(page, payload, selected):
                return True
            if attempt + 1 < attempts:
                self.log(
                    "[CGV] API 임시선점은 유지합니다 · 화면 좌석 상태를 재구성한 뒤 "
                    f"동기화를 다시 시도합니다 ({attempt + 2}/{attempts}).",
                    "warning",
                )
        return False

    def _watch_and_hold_api(
        self,
        page,
        schedule: dict[str, Any],
        groups: tuple[CgvSeatGroup, ...],
        people: int,
        developer_mode: bool,
        cgv: dict[str, Any],
    ) -> tuple[bool, bool]:
        self._last_fast_monitor_exit_reason = ""
        self._last_fast_retry_after_seconds = 0.0
        auth = self._browser_auth_data(page)
        initial_response = self._consume_initial_seat_response(schedule)
        seat_url = str(initial_response.get("url", "") or "") or self._seat_url(
            schedule, auth.get("custNo", "")
        )
        request_headers = initial_response.get("requestHeaders", {})
        if not isinstance(request_headers, dict):
            request_headers = {}
        initial_payload = initial_response.get("data")
        if not isinstance(initial_payload, dict):
            initial_payload = None
        if initial_payload is not None:
            self.log(
                "CGV 최초 좌석 응답 재사용 · 중복 조회 없이 즉시 선점 여부를 확인합니다.",
                "info",
            )
        direct_hold = self._direct_hold_config(schedule, people, auth, cgv)
        preferred_concurrency = max(
            1,
            min(self.scan_concurrency, int(self.FAST_SEAT_MAX_INFLIGHT)),
        )
        concurrency = preferred_concurrency
        launch_interval_ms = self.FAST_SEAT_LAUNCH_INTERVAL_MS
        backoff = self.MIN_POLL_INTERVAL
        conflict_limit = max(0, int(self._fast_monitor_conflict_limit()))
        while not self.stop_event.is_set():
            recovered_initial = {}
            started = self._start_fast_seat_monitor(
                page,
                seat_url,
                groups,
                concurrency,
                launch_interval_ms=launch_interval_ms,
                direct_hold=direct_hold,
                request_headers=request_headers,
                initial_payload=initial_payload,
                max_conflicts=conflict_limit,
            )
            # A captured response is a one-shot seed.  Never replay stale seat
            # availability if the base policy restarts a monitor after errors.
            initial_payload = None
            if not started:
                recovered_initial = self._recover_fast_monitor_snapshot(page, schedule, groups)
                if not recovered_initial:
                    self._stop_fast_seat_monitor(page)
                    self._last_fast_monitor_exit_reason = "hold-uncertain"
                    self.log("[CGV] 감시 설치 응답·실행 상태 미확인 · 중복 선점 없이 정지", "warning")
                    return False, False

            last_completed = 0
            snapshot: dict[str, Any] = {}
            try:
                while not self.stop_event.wait(self.FAST_MONITOR_READ_INTERVAL):
                    snapshot = recovered_initial or self._read_fast_seat_monitor(page)
                    recovered_initial = {}
                    if not snapshot:
                        snapshot = self._recover_fast_monitor_snapshot(page, schedule, groups)
                        if not snapshot:
                            snapshot = {"terminalError": "hold-uncertain"}
                            break
                    completed = max(0, int(snapshot.get("completed", 0) or 0))
                    if completed > last_completed:
                        self.silent_ticks(
                            completed - last_completed,
                            "선택한 CGV 좌석 묶음을 고속 API로 감시 중",
                        )
                        last_completed = completed
                    terminal = (
                        snapshot.get("hit")
                        or snapshot.get("blocked")
                        or snapshot.get("unauthorized")
                        or snapshot.get("terminalError")
                        or (
                            not snapshot.get("running", False)
                            and not snapshot.get("claiming", False)
                        )
                    )
                    if terminal:
                        if snapshot.get("terminalError") == "hold-uncertain":
                            recovered = self._recover_fast_monitor_snapshot(page, schedule, groups)
                            if recovered.get("hit"):
                                snapshot = recovered
                        break
            finally:
                self._stop_fast_seat_monitor(page)

            if self.stop_event.is_set():
                return False, False

            self._log_fast_monitor_timing(snapshot)
            hit = snapshot.get("hit") if isinstance(snapshot, dict) else None
            if not isinstance(hit, dict):
                status = int(snapshot.get("lastStatus", 0) or 0)
                failure_kind = str(snapshot.get("failureKind", "") or "")
                if failure_kind == "seat-conflict" or snapshot.get("terminalError"):
                    detail = redact_debug_text(
                        f"단계={snapshot.get('lastFailureStage', '')} · HTTP {status} · "
                        f"API {snapshot.get('lastApiStatus', '')} · 결과 {snapshot.get('lastResultCode', '')} · "
                        f"가격 {float(snapshot.get('priceElapsedMs', 0) or 0):.0f}ms · "
                        f"선점 {float(snapshot.get('holdElapsedMs', 0) or 0):.0f}ms · "
                        f"{snapshot.get('lastApiMessage', '')}",
                        extra_secrets=[v for v in auth.values() if isinstance(v, str) and len(v) > 3],
                    )
                    self.log(f"[CGV] 선점 실패 진단 · {detail[:300]}", "warning")
                if snapshot.get("terminalError") == "hold-uncertain":
                    self._last_fast_monitor_exit_reason = "hold-uncertain"
                    self.log("[CGV] 선점 요청의 결과가 불명확합니다 · 추가 선점 없이 중지합니다. "
                             "열린 CGV 화면과 예매내역을 확인해주세요.", "warning")
                    return False, False
                if failure_kind == "seat-conflict":
                    self._last_fast_monitor_exit_reason = "seat-conflict"
                    return False, False
                self._last_fast_retry_after_seconds = max(
                    0.0,
                    float(snapshot.get("retryAfterMs", 0) or 0) / 1000.0,
                )
                if snapshot.get("unauthorized") or status == 401:
                    self._last_fast_monitor_exit_reason = "unauthorized"
                    self.log("CGV 로그인 인증이 만료되어 브라우저 확인 경로로 전환합니다.", "warning")
                    return False, True
                if snapshot.get("terminalError"):
                    self._last_fast_monitor_exit_reason = str(
                        snapshot.get("terminalError") or "terminal-error"
                    )
                    if self._last_fast_monitor_exit_reason in {"price-rejected", "hold-rejected"}:
                        self.log("[CGV] 가격/선점 API가 요청을 거절했습니다 · 좌석 경합으로 간주하지 않고 "
                                 "공식 예매 화면으로 전환합니다.", "warning")
                    else:
                        self.log("CGV 선점 API 응답 구조가 변경되어 브라우저 안전 경로로 전환합니다.", "warning")
                    return False, True
                if snapshot.get("blocked") or status in {403, 429}:
                    concurrency = 1
                    launch_interval_ms = min(1000, max(400, launch_interval_ms * 2))
                    backoff = min(self.MAX_BACKOFF, max(3.0, backoff * 2))
                    self.silent_tick(f"CGV 좌석 API 연결 제한 · {backoff:.1f}초 후 재시도")
                else:
                    backoff = min(self.MAX_BACKOFF, max(0.5, backoff * 1.5))
                    detail = str(snapshot.get("lastError", "") or "").strip()
                    message = "CGV 고속 좌석 API 조회 오류"
                    if detail:
                        message += f" ({detail[:160]})"
                    self.silent_tick(message)
                self.stop_event.wait(backoff)
                continue

            seat_payload = hit.get("data", {})
            seats = parse_api_seats(seat_payload)
            chosen = self.choose_available_api_group(seats, groups)
            if not chosen:
                self.silent_tick("CGV 좌석 응답이 변경되어 고속 감시를 계속합니다")
                continue
            group, selected = chosen
            concurrency = preferred_concurrency
            launch_interval_ms = self.FAST_SEAT_LAUNCH_INTERVAL_MS
            backoff = self.MIN_POLL_INTERVAL
            detected_ms = float(hit.get("elapsedMs", 0.0) or 0.0)
            self.log(
                f"CGV 좌석 개방 감지: {', '.join(group.seats)} · API {detected_ms:.0f}ms",
                "success",
            )

            transaction = hit.get("transaction")
            if not isinstance(transaction, dict):
                self._last_fast_monitor_exit_reason = "missing-transaction"
                self.log("CGV 브라우저 내부 선점 결과가 없어 안전 경로로 전환합니다.", "warning")
                return False, True
            price_response = transaction.get("priceResponse")
            hold_response = transaction.get("holdResponse")
            hold_payload = transaction.get("holdPayload")
            if not all(
                isinstance(value, dict)
                for value in (price_response, hold_response, hold_payload)
            ):
                self._last_fast_monitor_exit_reason = "incomplete-transaction"
                self.log("CGV 브라우저 내부 선점 응답이 불완전해 안전 경로로 전환합니다.", "warning")
                return False, True
            transaction_ms = float(transaction.get("elapsedMs", 0.0) or 0.0)

            self.log(
                f"CGV 브라우저 내부 API 좌석 임시선점 완료: {', '.join(group.seats)} "
                f"· {transaction_ms:.0f}ms",
                "success",
            )
            if not self._prepare_api_hold_ui(page, schedule, people):
                self._cancel_api_hold(page, hold_payload, hold_response)
                self._last_fast_monitor_exit_reason = "held-schedule-ui-failed"
                self.log(
                    "CGV 확보 회차 화면 전환에 실패해 임시선점을 해제하고 안전 경로로 전환합니다.",
                    "warning",
                )
                return False, True
            if not self._sync_held_seats_for_checkout(page, seat_payload, selected):
                self._cancel_api_hold(page, hold_payload, hold_response)
                self._last_fast_monitor_exit_reason = "ui-sync-failed"
                self.log(
                    "CGV 화면 동기화 복구 한도를 모두 사용해 확보 좌석을 해제하고 "
                    "안전 경로로 전환합니다.",
                    "warning",
                )
                return False, True

            self._install_cached_hold_responses(page, price_response, hold_response)
            if self._submit_seat_selection(page):
                self._restore_fetch(page)
                if developer_mode:
                    self._developer_hold_cleanup = (
                        dict(hold_payload),
                        dict(hold_response),
                    )
                return True, False
            self._restore_fetch(page)
            self._cancel_api_hold(page, hold_payload, hold_response)
            self._last_fast_monitor_exit_reason = "checkout-transition-failed"
            self.log(
                "CGV 결제 화면 연결이 확인되지 않아 임시선점을 취소하고 브라우저 안전 경로로 전환합니다.",
                "warning",
            )
            return False, True
        return False, False

    @staticmethod
    def _safe_page_url(page) -> str:
        try:
            return str(page.url or "")
        except Exception:
            return ""

    @staticmethod
    def _is_naver_payment_url(url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            host = (parsed.hostname or "").casefold()
            path = parsed.path.casefold()
        except Exception:
            return False
        return (
            host == "pay.naver.com"
            or host.endswith(".pay.naver.com")
            or (host == "financial.pstatic.net" and "/instantpay/" in path)
        )

    def _wait_for_checkout_condition(
        self,
        page,
        condition,
        timeout_seconds: float,
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and not self.stop_event.is_set():
            try:
                if condition():
                    return True
            except Exception:
                pass
            try:
                page.wait_for_timeout(self.PAYMENT_POLL_INTERVAL_MS)
            except Exception:
                if self.stop_event.wait(self.PAYMENT_POLL_INTERVAL_MS / 1000.0):
                    break
        try:
            return bool(condition())
        except Exception:
            return False

    def _cgv_payment_methods_ready(self, page) -> bool:
        url = self._safe_page_url(page)
        if "/mpy/main" in urllib.parse.urlparse(url).path:
            return True
        try:
            return bool(
                page.evaluate(
                    r"""() => {
                      const text = document.body ? document.body.innerText || '' : '';
                      return text.includes('결제수단') && text.includes('최종결제금액');
                    }"""
                )
            )
        except Exception:
            return False

    @staticmethod
    def _click_enabled_payment_button(page) -> tuple[bool, str]:
        try:
            result = page.evaluate(
                r"""() => {
                  const clean = value => (value || '').replace(/\s+/g, '');
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const buttons = Array.from(document.querySelectorAll(
                    'button, a, [role="button"]'
                  )).filter(node => {
                    const text = clean(node.innerText || node.textContent);
                    return visible(node) && text.endsWith('결제하기') &&
                           !node.disabled && node.getAttribute('aria-disabled') !== 'true';
                  });
                  const target = buttons[buttons.length - 1];
                  if (!target) return {clicked: false, text: ''};
                  target.scrollIntoView({block: 'center'});
                  target.click();
                  return {clicked: true, text: clean(target.innerText || target.textContent)};
                }"""
            )
        except Exception:
            return False, ""
        if not isinstance(result, dict):
            return False, ""
        return bool(result.get("clicked")), str(result.get("text") or "")

    def _wait_and_click_payment_button(
        self,
        page,
        timeout_seconds: float,
    ) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and not self.stop_event.is_set():
            clicked, text = self._click_enabled_payment_button(page)
            if clicked:
                return True, text
            try:
                page.wait_for_timeout(self.PAYMENT_POLL_INTERVAL_MS)
            except Exception:
                if self.stop_event.wait(self.PAYMENT_POLL_INTERVAL_MS / 1000.0):
                    break
        return False, ""

    def _advance_to_cgv_payment_methods(self, page) -> bool:
        if self._cgv_payment_methods_ready(page):
            return True
        clicked, _text = self._wait_and_click_payment_button(
            page,
            self.NPAY_CONTROL_TIMEOUT_SECONDS,
        )
        if not clicked:
            self.log(
                "CGV 좌석 확인 화면의 첫 번째 '결제하기' 버튼을 찾지 못했습니다.",
                "warning",
            )
            return False
        self.log("[CGV] 좌석 확인 완료 · 결제수단 화면으로 이동 중...", "info")
        ready = self._wait_for_checkout_condition(
            page,
            lambda: self._cgv_payment_methods_ready(page),
            self.CGV_PAYMENT_PAGE_TIMEOUT_SECONDS,
        )
        if not ready:
            self.log(
                "CGV 결제수단 화면(/mpy/main) 진입을 확인하지 못했습니다.",
                "warning",
            )
        return ready

    def _select_cgv_npay_method(self, page) -> bool:
        clicked = False
        selected = False
        deadline = time.monotonic() + self.NPAY_CONTROL_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not self.stop_event.is_set():
            try:
                state = page.evaluate(
                    r"""allowClick => {
                      const clean = value => (value || '').replace(/\s+/g, '').toLowerCase();
                      const visible = node => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                               rect.width > 0 && rect.height > 0;
                      };
                      const buttons = Array.from(document.querySelectorAll('button'));
                      const target = buttons.find(button => {
                        const imageText = Array.from(button.querySelectorAll('img'))
                          .map(image => image.getAttribute('alt') || '').join(' ');
                        const text = clean([
                          button.innerText,
                          button.getAttribute('aria-label'),
                          imageText,
                        ].join(' '));
                        return visible(button) &&
                          (text.includes('npay') || text.includes('네이버페이'));
                      });
                      if (!target) return {found: false, selected: false, clicked: false};
                      const scope = target.closest('li') || target;
                      const classes = `${target.className || ''} ${scope.className || ''}`.toLowerCase();
                      const isSelected = clean(target.getAttribute('title')) === '선택됨' ||
                        target.getAttribute('aria-pressed') === 'true' ||
                        target.getAttribute('aria-checked') === 'true' ||
                        scope.getAttribute('data-selected') === 'true' ||
                        /(^|[\s_-])(active|selected|checked)([\s_-]|$)/.test(classes);
                      if (!isSelected && allowClick && !target.disabled &&
                          target.getAttribute('aria-disabled') !== 'true') {
                        target.scrollIntoView({block: 'center'});
                        target.click();
                        return {found: true, selected: false, clicked: true};
                      }
                      return {found: true, selected: isSelected, clicked: false};
                    }""",
                    not clicked,
                )
            except Exception:
                state = None
            if isinstance(state, dict):
                clicked = clicked or bool(state.get("clicked"))
                selected = bool(state.get("selected"))
                if selected:
                    self.log("[CGV] 결제수단 N pay 선택 완료", "info")
                    return True
            try:
                page.wait_for_timeout(self.PAYMENT_POLL_INTERVAL_MS)
            except Exception:
                if self.stop_event.wait(self.PAYMENT_POLL_INTERVAL_MS / 1000.0):
                    break
        self.log(
            "CGV 결제수단 N pay를 선택했지만 선택 상태를 확인하지 못했습니다."
            if clicked
            else "CGV 결제수단에서 N pay 버튼을 찾지 못했습니다.",
            "warning",
        )
        return False

    def _accept_cgv_payment_terms(self, page) -> bool:
        clicked = False
        deadline = time.monotonic() + self.NPAY_CONTROL_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not self.stop_event.is_set():
            try:
                state = page.evaluate(
                    r"""allowClick => {
                      const checkbox = document.querySelector('input#chkAll[type="checkbox"]');
                      if (!checkbox) return {found: false, checked: false, clicked: false};
                      if (checkbox.checked) return {found: true, checked: true, clicked: false};
                      if (!allowClick || checkbox.disabled) {
                        return {found: true, checked: false, clicked: false};
                      }
                      const label = document.querySelector('label[for="chkAll"]');
                      (label || checkbox).click();
                      return {found: true, checked: false, clicked: true};
                    }""",
                    not clicked,
                )
            except Exception:
                state = None
            if isinstance(state, dict):
                clicked = clicked or bool(state.get("clicked"))
                if state.get("checked"):
                    self.log("[CGV] 필수 약관 전체 동의 완료", "info")
                    return True
            try:
                page.wait_for_timeout(self.PAYMENT_POLL_INTERVAL_MS)
            except Exception:
                if self.stop_event.wait(self.PAYMENT_POLL_INTERVAL_MS / 1000.0):
                    break
        self.log(
            "CGV 필수 약관 전체 동의 상태를 확인하지 못했습니다.",
            "warning",
        )
        return False

    def _find_naver_payment_page(self, page):
        try:
            context = page.context
        except Exception:
            context = None
        deadline = time.monotonic() + self.NPAY_PAGE_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not self.stop_event.is_set():
            pages = []
            if context is not None:
                try:
                    pages.extend(list(context.pages))
                except Exception:
                    pass
            if page not in pages:
                pages.append(page)
            for candidate in reversed(pages):
                try:
                    if candidate.is_closed():
                        continue
                except Exception:
                    pass
                if self._is_naver_payment_url(self._safe_page_url(candidate)):
                    try:
                        candidate.bring_to_front()
                        candidate.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    return candidate
            try:
                page.wait_for_timeout(self.PAYMENT_POLL_INTERVAL_MS)
            except Exception:
                if self.stop_event.wait(self.PAYMENT_POLL_INTERVAL_MS / 1000.0):
                    break
        return None

    def _open_naver_payment_page(self, page):
        self.log("[CGV] N pay 약관 확인 완료 · 네이버페이 페이지 호출 중...", "info")
        clicked, _text = self._wait_and_click_payment_button(
            page,
            self.NPAY_CONTROL_TIMEOUT_SECONDS,
        )
        if not clicked:
            self.log(
                "CGV 결제수단 화면의 두 번째 '결제하기' 버튼이 비활성화됐거나 보이지 않습니다.",
                "warning",
            )
            return None
        naver_page = self._find_naver_payment_page(page)
        if naver_page is None:
            self.log(
                "네이버페이 페이지 전환을 확인하지 못했습니다. 예매 성공으로 처리하지 않습니다.",
                "warning",
            )
            return None
        self.log("[CGV] 네이버페이 페이지 로드 완료", "info")
        return naver_page

    @staticmethod
    def _is_naver_login_url(url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            host = (parsed.hostname or "").casefold()
            path = parsed.path.casefold()
        except Exception:
            return False
        return host == "nid.naver.com" and "nidlogin" in path

    @staticmethod
    def _click_prefilled_naver_login(page, allow_click: bool = True) -> dict[str, bool]:
        """Click Naver login only when the saved browser credentials are filled.

        The credential values never leave the page. Only boolean presence flags
        are returned to Python so logs and crash reports cannot expose them.
        """

        try:
            result = page.evaluate(
                r"""allowClick => {
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const idInput = document.querySelector('input#id[name="id"]');
                  const pwInput = document.querySelector('input#pw[name="pw"]');
                  const buttons = [
                    document.querySelector('#loginBtn_row'),
                    document.querySelector('#loginBtn_column'),
                  ].filter(button => visible(button));
                  const button = buttons[0] || null;
                  const hasId = !!(idInput && String(idInput.value || '').trim());
                  const hasPassword = !!(pwInput && String(pwInput.value || ''));
                  const ready = !!button && hasId && hasPassword &&
                    !button.disabled && button.getAttribute('aria-disabled') !== 'true';
                  if (ready && allowClick) {
                    button.scrollIntoView({block: 'center'});
                    button.click();
                    return {found: true, filled: true, clicked: true};
                  }
                  return {
                    found: !!(idInput && pwInput && button),
                    filled: hasId && hasPassword,
                    clicked: false,
                  };
                }""",
                allow_click,
            )
        except Exception:
            return {"found": False, "filled": False, "clicked": False}
        if not isinstance(result, dict):
            return {"found": False, "filled": False, "clicked": False}
        return {
            "found": bool(result.get("found")),
            "filled": bool(result.get("filled")),
            "clicked": bool(result.get("clicked")),
        }

    @staticmethod
    def _naver_additional_verification_visible(page) -> bool:
        try:
            return bool(
                page.evaluate(
                    r"""() => {
                      const text = (document.body && document.body.innerText || '')
                        .replace(/\s+/g, '');
                      return text.includes('보안을위해추가확인을해주세요') ||
                        text.includes('자동입력방지') ||
                        text.includes('보안문자') ||
                        text.includes('2단계인증') ||
                        text.includes('본인확인');
                    }"""
                )
            )
        except Exception:
            return False

    def _ensure_naver_payment_session(self, page):
        """Resume N pay after the dedicated browser is sent through Naver login."""

        initial_url = self._safe_page_url(page)
        if not (
            self._is_naver_payment_url(initial_url)
            or self._is_naver_login_url(initial_url)
        ):
            # Compatibility for test doubles and already-normalized wrappers.
            return page

        deadline = time.monotonic() + self.NPAY_PAGE_TIMEOUT_SECONDS
        login_clicked = False
        missing_credentials_reported = False
        additional_verification_reported = False
        payment_seen_at: float | None = None
        while time.monotonic() < deadline and not self.stop_event.is_set():
            candidates = [page]
            try:
                page_closed = page.is_closed()
            except Exception:
                page_closed = False
            if page_closed:
                try:
                    candidates = [
                        candidate
                        for candidate in reversed(list(page.context.pages))
                        if not candidate.is_closed()
                        and (
                            self._is_naver_login_url(self._safe_page_url(candidate))
                            or self._is_naver_payment_url(self._safe_page_url(candidate))
                        )
                    ]
                except Exception:
                    candidates = []

            for candidate in candidates:
                try:
                    if candidate.is_closed():
                        continue
                except Exception:
                    pass
                url = self._safe_page_url(candidate)
                if self._is_naver_login_url(url):
                    payment_seen_at = None
                    state = self._click_prefilled_naver_login(
                        candidate,
                        allow_click=not login_clicked,
                    )
                    if state.get("clicked"):
                        login_clicked = True
                        self.log(
                            "[CGV] 네이버 재로그인 화면 감지 · 저장된 입력으로 로그인 버튼 클릭 완료",
                            "info",
                        )
                    elif state.get("found") and not state.get("filled"):
                        if not missing_credentials_reported:
                            missing_credentials_reported = True
                            self.log(
                                "[CGV] 네이버 재로그인이 필요하지만 저장된 입력이 없습니다. "
                                "열린 Chrome에서 로그인하면 결제를 자동으로 계속합니다.",
                                "warning",
                            )
                    elif (
                        self._naver_additional_verification_visible(candidate)
                        and not additional_verification_reported
                    ):
                        additional_verification_reported = True
                        # CGV already owns a bounded seat hold at this point.
                        # Give the user the full confirmation grace instead of
                        # abandoning it at the shorter page-load deadline.
                        deadline = max(
                            deadline,
                            time.monotonic() + self.NPAY_COMPLETION_TIMEOUT_SECONDS,
                        )
                        self.log(
                            "[CGV] 네이버가 추가 보안 확인을 요구했습니다. "
                            "열린 Chrome에서 확인을 완료하면 N pay 결제를 자동으로 계속합니다.",
                            "warning",
                        )
                    continue
                if self._is_naver_payment_url(url):
                    now = time.monotonic()
                    if payment_seen_at is None:
                        payment_seen_at = now
                    # Give the payment bootstrap a short window to redirect to
                    # nid.naver.com before treating this as an authenticated page.
                    if now - payment_seen_at >= 0.75:
                        try:
                            candidate.bring_to_front()
                        except Exception:
                            pass
                        if login_clicked:
                            self.log(
                                "[CGV] 네이버 재로그인 완료 · N pay 결제 흐름 자동 복귀",
                                "success",
                            )
                        return candidate

            try:
                page.wait_for_timeout(self.PAYMENT_POLL_INTERVAL_MS)
            except Exception:
                if self.stop_event.wait(self.PAYMENT_POLL_INTERVAL_MS / 1000.0):
                    break

        self.log(
            "네이버 재로그인 또는 N pay 결제 화면 복귀를 제한시간 안에 확인하지 못했습니다. "
            "추가 인증 화면을 확인해주세요.",
            "warning",
        )
        return None

    @staticmethod
    def _naver_payment_button_state(page) -> dict[str, Any]:
        try:
            result = page.evaluate(
                r"""() => {
                  const clean = value => (value || '').replace(/\s+/g, '');
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const pattern = /^(?:[\d,]+원)?(?:동의하고)?결제하기$/;
                  const buttons = Array.from(document.querySelectorAll('button'))
                    .filter(button => visible(button) && pattern.test(clean(button.innerText)));
                  const button = buttons[buttons.length - 1];
                  if (!button) return {found: false, enabled: false, text: ''};
                  return {
                    found: true,
                    enabled: !button.disabled && button.getAttribute('aria-disabled') !== 'true',
                    text: clean(button.innerText),
                  };
                }"""
            )
        except Exception:
            return {"found": False, "enabled": False, "text": ""}
        return result if isinstance(result, dict) else {
            "found": False,
            "enabled": False,
            "text": "",
        }

    @staticmethod
    def _select_first_naver_card(page) -> str:
        try:
            return str(
                page.evaluate(
                    r"""() => {
                      const clean = value => (value || '').replace(/\s+/g, ' ').trim();
                      const visible = node => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                               rect.width > 0 && rect.height > 0;
                      };
                      const headings = Array.from(document.querySelectorAll('strong'))
                        .filter(node => visible(node) && clean(node.innerText) === '카드');
                      for (const heading of headings) {
                        let sibling = heading.nextElementSibling;
                        while (sibling) {
                          if (sibling.matches('strong') && clean(sibling.innerText) === '계좌') break;
                          const buttons = Array.from(sibling.querySelectorAll('button'))
                            .filter(button => visible(button) &&
                              !clean(button.innerText).includes('점검중'));
                          if (buttons.length) {
                            const target = buttons[0];
                            const label = clean(target.innerText);
                            target.click();
                            return label || '저장 카드';
                          }
                          sibling = sibling.nextElementSibling;
                        }
                      }
                      const dialog = Array.from(document.querySelectorAll('[role="dialog"], section, div'))
                        .find(node => visible(node) &&
                          clean(node.innerText).includes('결제수단 전체보기'));
                      if (dialog && !headings.length) {
                        const candidates = Array.from(dialog.querySelectorAll('li button'))
                          .filter(button => {
                            const text = clean(button.innerText);
                            const classText = Array.from(button.querySelectorAll('[class]'))
                              .map(node => node.className || '').join(' ').toLowerCase();
                            const hasCardMark = !!button.querySelector('img[alt$=" 로고"]') ||
                              /(^|[_-])card([_-]|$)/.test(classText);
                            return visible(button) && !text.includes('추가하기') &&
                              !text.includes('점검중') && hasCardMark;
                          });
                        if (candidates.length) {
                          const target = candidates[0];
                          const label = clean(target.innerText);
                          target.click();
                          return label || '저장 카드';
                        }
                      }
                      const allButtons = Array.from(document.querySelectorAll('button'));
                      const fullView = allButtons.find(button =>
                        visible(button) && clean(button.innerText) === '결제수단 전체보기'
                      );
                      if (fullView) {
                        fullView.click();
                        return '__opened__';
                      }
                      return '';
                    }"""
                )
                or ""
            )
        except Exception:
            return ""

    def _prepare_naver_card(self, page) -> bool:
        deadline = time.monotonic() + self.NPAY_CONTROL_TIMEOUT_SECONDS
        opened_card_list = False
        selected_card = ""
        while time.monotonic() < deadline and not self.stop_event.is_set():
            state = self._naver_payment_button_state(page)
            if selected_card and state.get("found") and state.get("enabled"):
                self.log(
                    f"[CGV] 네이버페이 저장 카드 선택 완료: {selected_card}",
                    "info",
                )
                return True
            selection = self._select_first_naver_card(page)
            if selection == "__opened__":
                opened_card_list = True
            elif selection:
                selected_card = selection
            elif (
                not opened_card_list
                and state.get("found")
                and state.get("enabled")
            ):
                self.log(
                    "[CGV] 네이버페이의 저장된 기본 카드 결제수단을 확인했습니다.",
                    "info",
                )
                return True
            try:
                page.wait_for_timeout(self.PAYMENT_POLL_INTERVAL_MS)
            except Exception:
                if self.stop_event.wait(self.PAYMENT_POLL_INTERVAL_MS / 1000.0):
                    break
        detail = (
            "사용 가능한 저장 카드를 선택하지 못했습니다."
            if opened_card_list
            else "네이버페이 결제 UI 또는 활성 결제 버튼을 확인하지 못했습니다."
        )
        self.log(f"{detail} 브라우저에서 직접 결제수단을 확인해주세요.", "warning")
        return False

    def _click_naver_final_payment(self, page) -> bool:
        clicked, text = self._wait_and_click_payment_button(
            page,
            self.NPAY_CONTROL_TIMEOUT_SECONDS,
        )
        if not clicked:
            self.log(
                "네이버페이의 마지막 '결제하기' 버튼을 찾지 못했습니다.",
                "warning",
            )
            return False
        self.log(f"[CGV] 네이버페이 마지막 '{text or '결제하기'}' 클릭 완료", "info")
        return True

    def _enter_naver_pay_password(self, page, npay_password: str) -> bool:
        """Locates the Naver Pay virtual keypad on page, visually matches 0-9 digits, and clicks them."""
        try:
            self.log("[CGV] 네이버페이 가상 보안 키패드 감지 · 6자리 비밀번호 자동 입력을 시작합니다.", "info")
            keypad_selectors = [
                "table[class*='SecureKeyboard_keyboard']",
                "table[class*='keyboard']",
                "table[class*='Secure']",
                "[class*='SecureKeyboard'] table",
            ]
            keypad_loc = None
            for sel in keypad_selectors:
                loc = page.locator(sel).first
                try:
                    if loc.is_visible(timeout=1000):
                        keypad_loc = loc
                        break
                except Exception:
                    continue

            if keypad_loc is None:
                screenshot_bytes = page.screenshot()
            else:
                screenshot_bytes = keypad_loc.screenshot()

            img = Image.open(io.BytesIO(screenshot_bytes))
            cells = NpayKeypadRecognizer.recognize_keypad_image(img)

            missing_digits = [d for d in npay_password if d not in cells]
            if missing_digits:
                self.log(
                    f"[CGV] 키패드에서 일부 숫자({missing_digits})를 인식하지 못했습니다. 수동 입력을 진행해주세요.",
                    "warning",
                )
                return False

            for digit in npay_password:
                cell = cells[digit]
                if keypad_loc is not None:
                    cell_btn = keypad_loc.locator(
                        f"tr:nth-child({cell.row}) td:nth-child({cell.col}) button"
                    ).first
                    try:
                        if cell_btn.is_visible(timeout=500):
                            cell_btn.click()
                        else:
                            keypad_loc.click(position={"x": cell.center[0], "y": cell.center[1]})
                    except Exception:
                        keypad_loc.click(position={"x": cell.center[0], "y": cell.center[1]})
                else:
                    page.mouse.click(cell.center[0], cell.center[1])

                page.wait_for_timeout(80)

            self.log("[CGV] 네이버페이 결제 비밀번호 6자리 자동 입력 완료", "success")
            return True
        except Exception as exc:
            self.log(
                f"[CGV] 네이버페이 가상 키패드 입력 중 오류: {format_exception(exc)}",
                "warning",
            )
            return False

    def _wait_for_cgv_payment_confirmation(
        self, cgv_page, naver_page, npay_password: str = ""
    ) -> bool:
        auth_reported = False
        password_entered = False
        deadline = time.monotonic() + self.NPAY_COMPLETION_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not self.stop_event.is_set():
            pages = []
            for base_page in (cgv_page, naver_page):
                try:
                    context_pages = list(base_page.context.pages)
                except Exception:
                    context_pages = []
                pages.extend(context_pages)
                if base_page not in pages:
                    pages.append(base_page)
            unique_pages = []
            for candidate in pages:
                if candidate not in unique_pages:
                    unique_pages.append(candidate)
            for candidate in reversed(unique_pages):
                try:
                    if candidate.is_closed():
                        continue
                except Exception:
                    pass
                url = self._safe_page_url(candidate)
                try:
                    body = candidate.locator("body").inner_text(timeout=1000)
                except Exception:
                    body = ""
                try:
                    parsed = urllib.parse.urlparse(url)
                    host = (parsed.hostname or "").casefold()
                    path = parsed.path.casefold()
                except Exception:
                    host = ""
                    path = ""
                success_by_url = host.endswith("cgv.co.kr") and (
                    "/mpy/purchase/" in path or "/complete" in path
                )
                if success_by_url:
                    self.log("[CGV] 네이버페이 결제 및 CGV 예매 완료 확인", "success")
                    return True
                if any(
                    phrase in body
                    for phrase in (
                        "결제에 실패했습니다",
                        "결제가 취소되었습니다",
                        "결제를 취소했습니다",
                        "결제 처리에 실패",
                    )
                ):
                    self.log(
                        "네이버페이에서 결제 실패 또는 취소 응답을 확인했습니다.",
                        "warning",
                    )
                    return False

                # Handle virtual keypad password entry
                if not password_entered:
                    keypad_found = False
                    for sel in (
                        "table[class*='SecureKeyboard_keyboard']",
                        "table[class*='keyboard']",
                        "table[class*='Secure']",
                    ):
                        try:
                            if candidate.locator(sel).first.is_visible(timeout=300):
                                keypad_found = True
                                break
                        except Exception:
                            continue

                    if keypad_found or "/pw/check" in path:
                        if npay_password and len(npay_password) == 6 and npay_password.isdigit():
                            if self._enter_naver_pay_password(candidate, npay_password):
                                password_entered = True
                        elif not auth_reported:
                            auth_reported = True
                            self.log(
                                "[CGV] 네이버페이 결제 비밀번호 입력이 필요합니다. 열린 Chrome에서 입력해주세요.",
                                "warning",
                            )

                if not auth_reported and any(
                    phrase in body
                    for phrase in ("결제 비밀번호", "비밀번호 입력", "본인인증", "생체인증")
                ):
                    auth_reported = True
                    self.log(
                        "[CGV] 네이버페이 본인인증이 필요합니다. 열린 Chrome에서 인증하면 완료 상태를 계속 확인합니다.",
                        "warning",
                    )
            try:
                naver_page.wait_for_timeout(self.PAYMENT_POLL_INTERVAL_MS)
            except Exception:
                if self.stop_event.wait(self.PAYMENT_POLL_INTERVAL_MS / 1000.0):
                    break
        self.log(
            "네이버페이 결제 요청 후 CGV 예매 완료 화면을 확인하지 못했습니다. "
            "예매 성공으로 처리하지 않으며 열린 브라우저에서 상태를 확인해주세요.",
            "warning",
        )
        return False

    def _proceed_naver_pay_checkout(
        self, page, developer_mode: bool = False, npay_password: str = ""
    ) -> bool:
        """Run the two CGV checkout steps, then drive the official Naver Pay page."""
        try:
            self.log("[CGV] 좌석 선점 완료 · 네이버페이 결제 흐름을 시작합니다.", "info")
            if not self._advance_to_cgv_payment_methods(page):
                return False
            if not self._select_cgv_npay_method(page):
                return False
            if not self._accept_cgv_payment_terms(page):
                return False
            naver_pay_page = self._open_naver_payment_page(page)
            if naver_pay_page is None:
                return False
            naver_pay_page = self._ensure_naver_payment_session(naver_pay_page)
            if naver_pay_page is None:
                return False
            if developer_mode:
                self.log(
                    "[개발자 모드] 네이버페이 페이지까지 정상 진입했습니다.",
                    "success",
                )
                self.log(
                    "[개발자 모드] 실제 결제 방지를 위해 카드 선택과 마지막 '결제하기'는 실행하지 않습니다.",
                    "warning",
                )
                return True
            if not self._prepare_naver_card(naver_pay_page):
                return False
            if not self._click_naver_final_payment(naver_pay_page):
                return False
            if npay_password:
                return self._wait_for_cgv_payment_confirmation(
                    page, naver_pay_page, npay_password
                )
            return self._wait_for_cgv_payment_confirmation(page, naver_pay_page)
        except Exception as exc:
            self.log(
                f"CGV 네이버페이 결제 진행 중 오류: {format_exception(exc)}",
                "warning",
            )
            return False

    def _report_checkout_outcome(
        self,
        *,
        checkout_completed: bool,
        developer_mode: bool,
        site_no: str,
        movie: str,
    ) -> bool:
        if not checkout_completed:
            self.log(
                "CGV 좌석은 임시선점했지만 결제 완료를 확인하지 못했습니다. "
                "예매 성공으로 처리하지 않으며 열린 Chrome에서 결제를 확인해주세요.",
                "warning",
            )
            return False
        if developer_mode:
            message = (
                "개발자 테스트 모드: 네이버페이 페이지까지 정상 진입했습니다 "
                "(최종 결제 미실행)."
            )
        else:
            message = "CGV 네이버페이 결제 및 예매 완료를 확인했습니다."
        result = BookingResult(
            True,
            message,
            details={"site_no": site_no, "movie": movie},
        )
        if self.notify_success(result):
            self.log(result.message, "success")
            return True
        return False

    def _finish_held_checkout(
        self,
        page,
        *,
        developer_mode: bool,
        npay_password: str,
        site_no: str,
        movie: str,
    ) -> bool:
        """Complete checkout reporting and always release a developer API hold."""

        try:
            checkout_completed = self._proceed_naver_pay_checkout(
                page,
                developer_mode=developer_mode,
                npay_password=npay_password,
            )
            return self._report_checkout_outcome(
                checkout_completed=checkout_completed,
                developer_mode=developer_mode,
                site_no=site_no,
                movie=movie,
            )
        finally:
            if developer_mode:
                self._release_developer_api_hold(page)

    @staticmethod
    def _query_payload(schedule: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "coCd", "siteNo", "scnsNo", "scnYmd", "scnSseq", "scnsrtTm",
            "scnendTm", "prodNo", "salsTznCd", "movkndCd", "tcscnsGradCd",
            "sascnsGradCd", "movTirCd", "siteGradCd", "srvltKindCd", "movfNo",
            "prdcmpTypCd", "prdtypCd", "prddtlTypCd", "dblfrNo", "dblfrRpsntYn",
            "videoAddexpCd", "sbtdivCd", "bzplcNo", "cxprdYn", "scnsGradCd",
            "speclIndctTypCd", "prcrulDivCd", "cratgClsCd", "cndSalYnList",
            "vatincYn", "slddKindCd", "iceconYn", "arthsYn", "srlsYn",
            "childnMovYn", "movNo", "movNm", "prmddNo", "prodImg",
            "movkndDsplEnm", "expoProdNm",
        )
        payload = {key: schedule.get(key, "") for key in keys}
        payload["coCd"] = payload.get("coCd") or CGV_COMPANY_CODE
        payload["salsTznCd"] = payload.get("salsTznCd") or "26"
        payload["soldierJoinStus"] = "N"
        return payload

    def _enter_visitor_page(self, page, schedule: dict[str, Any]) -> bool:
        try:
            payload = self._query_payload(schedule)
            page.evaluate(
                "payload => { sessionStorage.setItem('query', JSON.stringify(payload)); "
                "sessionStorage.setItem('rsrtHistoryBack', 'Y'); }",
                payload,
            )
            page.goto(f"{CGV_HOME_URL}/cnm/selectVisitorCnt", wait_until="domcontentloaded", timeout=30000)
            if "/mem/login" in page.url:
                self.log(
                    "열린 CGV Chrome에서 로그인해주세요. 로그인 완료를 감지하면 자동으로 계속합니다.",
                    "warning",
                )
                while not self.stop_event.is_set() and "/mem/login" in page.url:
                    page.wait_for_timeout(500)
                if self.stop_event.is_set():
                    return False
                page.evaluate(
                    "payload => sessionStorage.setItem('query', JSON.stringify(payload))", payload
                )
                page.goto(
                    f"{CGV_HOME_URL}/cnm/selectVisitorCnt",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            return not self._is_block_page(page)
        except Exception:
            return False

    def _prepare_published_schedule(
        self,
        page,
        schedule: dict[str, Any],
        people: int,
    ) -> bool:
        """Bound retries while CGV finishes publishing the visitor/seat UI."""

        attempts = max(1, int(self.SCHEDULE_PROMOTION_ATTEMPTS))
        for attempt in range(attempts):
            if self.stop_event.is_set():
                return False

            visitors_ready = False
            if self._enter_visitor_page(page, schedule):
                seat_capture_handler = self._begin_initial_seat_response_capture(page)
                try:
                    visitors_ready = self._select_visitors(page, people)
                finally:
                    self._end_initial_seat_response_capture(
                        page, seat_capture_handler
                    )
            if visitors_ready:
                self._api_hold_ui_schedule_key = tuple(
                    str(schedule.get(key, "") or "")
                    for key in ("siteNo", "scnYmd", "scnsNo", "scnSseq")
                )
                return True

            # Never leak a response captured from an incomplete render into the
            # next official schedule/seat attempt.
            self._initial_seat_response = None
            if attempt + 1 >= attempts:
                break
            self.silent_tick(
                "CGV 회차 화면 부분 공개/렌더 지연 · "
                f"화면 준비 재시도 {attempt + 2}/{attempts}"
            )
            if self.stop_event.wait(
                max(0.0, float(self.SCHEDULE_PROMOTION_RETRY_INTERVAL))
            ):
                return False
        return False

    @staticmethod
    def _click_visible_by_text(page, labels: tuple[str, ...]) -> bool:
        for label in labels:
            for btn_loc in (
                page.locator("button, a, div[role='button']").filter(has_text=label),
                page.locator(f"button:has-text('{label}'), a:has-text('{label}')"),
                page.get_by_text(label, exact=True),
                page.get_by_text(label, exact=False),
            ):
                try:
                    for index in range(btn_loc.count()):
                        candidate = btn_loc.nth(index)
                        if candidate.is_visible():
                            try:
                                candidate.click(timeout=2500)
                                return True
                            except Exception:
                                candidate.click(force=True, timeout=1500)
                                return True
                except Exception:
                    continue

            # Fallback to JavaScript DOM evaluation
            try:
                clicked = page.evaluate(
                    r"""
                    label => {
                      const clean = s => (s || '').replace(/\s+/g, '');
                      const targetClean = clean(label);
                      const elements = [...document.querySelectorAll('button, a, div[role="button"], span, div')];
                      for (const el of elements) {
                        if (clean(el.textContent) === targetClean || (el.textContent || '').trim() === label) {
                          const clickable = el.closest('button, a, div[role="button"]') || el;
                          clickable.scrollIntoView({block: 'center', inline: 'center'});
                          clickable.click();
                          return true;
                        }
                      }
                      return false;
                    }
                    """,
                    label,
                )
                if clicked:
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _seat_modal_snapshot(page) -> dict[str, Any]:
        """Return separate readiness signals for the modal shell and seat data."""

        try:
            result = page.evaluate(
                r"""
                () => {
                  const clean = value => (value || '').replace(/\s+/g, '');
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const seatButtons = Array.from(
                    document.querySelectorAll('button[data-seatlocno]')
                  ).filter(visible);
                  const controls = Array.from(
                    document.querySelectorAll('button, a, div[role="button"]')
                  ).filter(visible);
                  const hasControl = label => controls.some(
                    node => clean(node.textContent) === label
                  );
                  return {
                    modalOpen: seatButtons.length > 0 ||
                               hasControl('인원변경') || hasControl('선택완료'),
                    seatCount: seatButtons.length,
                  };
                }
                """
            )
            return dict(result) if isinstance(result, dict) else {}
        except Exception:
            return {}

    def _select_visitors(self, page, people: int) -> bool:
        """Select visitors, open the seat modal, and wait for actual seat data."""
        start_time = time.monotonic()
        target_num = max(1, people)
        last_snapshot: dict[str, Any] = {}

        while (
            not self.stop_event.is_set()
            and time.monotonic() - start_time < self.VISITOR_SELECTION_TIMEOUT
        ):
            last_snapshot = self._seat_modal_snapshot(page)
            if int(last_snapshot.get("seatCount", 0) or 0) > 0:
                return True

            # Once the shell is open, repeatedly clicking the hidden visitor
            # controls only triggers more seat requests.  Wait for the existing
            # request to finish (or for the caller's rate-limit recovery).
            if not last_snapshot.get("modalOpen"):
                try:
                    page.evaluate(
                        r"""
                        people => {
                          const clean = value => (value || '').replace(/\s+/g, '');
                          const nodes = [...document.querySelectorAll('*')];
                          const label = nodes.find(node => node.children.length === 0 && clean(node.textContent) === '일반');
                          if (!label) return false;
                          let box = label;
                          for (let i = 0; i < 7 && box; i++, box = box.parentElement) {
                            const target = [...box.querySelectorAll('button')].find(button =>
                              !button.disabled && clean(button.textContent) === String(people)
                            );
                            if (target) {
                              target.click();
                              return true;
                            }
                          }
                          return false;
                        }
                        """,
                        target_num,
                    )
                except Exception:
                    pass

                try:
                    page.evaluate(
                        r"""
                        () => {
                          const clean = value => (value || '').replace(/\s+/g, '');
                          const buttons = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
                          const target = buttons.find(b => !b.disabled && clean(b.textContent) === '선택');
                          if (target) {
                            target.click();
                            return true;
                          }
                          return false;
                        }
                        """
                    )
                except Exception:
                    pass

            page.wait_for_timeout(self.VISITOR_RETRY_INTERVAL_MS)

        last_snapshot = self._seat_modal_snapshot(page) or last_snapshot
        if int(last_snapshot.get("seatCount", 0) or 0) > 0:
            return True
        if last_snapshot.get("modalOpen"):
            self.log(
                "CGV 좌석 모달은 열렸지만 좌석 데이터가 제한 시간 안에 로드되지 않았습니다.",
                "warning",
            )
        else:
            self.log("CGV 관람 인원 선택 및 좌석 모달 열기에 실패했습니다.", "error")
        return False

    @staticmethod
    def _available_seat_elements(page) -> list[dict[str, Any]]:
        try:
            result = page.evaluate(
                r"""
                () => [...document.querySelectorAll('button[data-seatlocno]')].map(node => {
                  const label = node.textContent || '';
                  const classes = String(node.className || '').toLowerCase();
                  const tokens = new Set(classes.split(/[\s_\-]+/));
                  const isSelected = node.getAttribute('aria-pressed') === 'true' ||
                                     node.getAttribute('aria-selected') === 'true' ||
                                     node.title === '선택됨' ||
                                     tokens.has('selected') || tokens.has('active') || tokens.has('on') ||
                                     (node.classList && (node.classList.contains('selected') || node.classList.contains('active') || node.classList.contains('on')));
                  const isUnavailable = !isSelected && (
                      node.disabled ||
                      node.getAttribute('aria-disabled') === 'true' ||
                      ['disabled', 'complete', 'sold', 'reserved', 'finish', 'soldout'].some(k => tokens.has(k) || classes.includes(k))
                  );
                  const isAvailable = !isUnavailable && !isSelected;
                  return {
                    id: node.getAttribute('data-seatlocno') || '',
                    label,
                    available: isAvailable,
                    selected: isSelected,
                    unavailable: isUnavailable,
                  };
                })
                """
            )
            return [dict(item) for item in result if isinstance(item, dict)] if isinstance(result, list) else []
        except Exception:
            return []

    @staticmethod
    def choose_available_group(
        elements: list[dict[str, Any]], groups: tuple[CgvSeatGroup, ...]
    ) -> tuple[CgvSeatGroup, dict[str, str]] | None:
        available: dict[str, str] = {}
        for element in elements:
            if element.get("unavailable"):
                continue
            label = str(element.get("label", ""))
            seat_id = str(element.get("id", ""))
            for match in re.finditer(r"([A-Za-z가-힣]+)\s*[-_ ]?\s*([0-9]+)", label):
                row_str, num_str = match.groups()
                available[normalize_seat_name(f"{row_str}{num_str}")] = seat_id
                try:
                    num_int = int(num_str)
                    available[normalize_seat_name(f"{row_str}{num_int}")] = seat_id
                    available[normalize_seat_name(f"{row_str}{num_int:02d}")] = seat_id
                except (ValueError, TypeError):
                    pass
        for group in groups:
            norm_seats = tuple(normalize_seat_name(seat) for seat in group.seats)
            if is_contiguous_seat_group(norm_seats) and all(
                s in available for s in norm_seats
            ):
                return group, {seat: available[normalize_seat_name(seat)] for seat in group.seats}
        return None

    def _submit_seat_selection(self, page) -> bool:
        """Click '선택완료' in the seat map modal and wait for page transition."""
        clicked = False
        try:
            clicked = bool(
                page.evaluate(
                    r"""
                    () => {
                      const clean = s => (s || '').replace(/\s+/g, '');
                      const visible = node => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                               rect.width > 0 && rect.height > 0;
                      };
                      const buttons = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
                      const target = buttons.find(b => clean(b.textContent) === '선택완료' &&
                        visible(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true');
                      if (target) {
                        if (typeof target.scrollIntoView === 'function') target.scrollIntoView({block: 'center'});
                        target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                        target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                        target.click();
                        return true;
                      }
                      return false;
                    }
                    """
                )
            )
        except Exception:
            clicked = False

        if not clicked:
            for loc in (
                page.locator("button:has-text('선택완료'), a:has-text('선택완료')"),
                page.get_by_text("선택완료", exact=True),
                page.get_by_text("선택 완료", exact=True),
            ):
                try:
                    for idx in range(loc.count()):
                        btn = loc.nth(idx)
                        if (
                            btn.is_visible()
                            and btn.is_enabled()
                            and btn.get_attribute("aria-disabled") != "true"
                        ):
                            btn.click(force=True, timeout=1500)
                            clicked = True
                            break
                    if clicked:
                        break
                except Exception:
                    continue

        if not clicked:
            return False

        def check_conflict() -> bool:
            try:
                return bool(
                    page.evaluate(
                        r"""
                        () => {
                          const text = (document.body.innerText || '');
                          return text.includes('이미 선택된') || text.includes('다른 고객이') ||
                                 text.includes('예매 중인 좌석') || text.includes('예매중인 좌석') ||
                                 text.includes('선점된 좌석') || text.includes('선택하신 좌석') ||
                                 text.includes('선택된 좌석이') || text.includes('이미 예매');
                        }
                        """
                    )
                )
            except Exception:
                return False

        def check_transition() -> bool:
            try:
                return bool(
                    page.evaluate(
                        r"""
                        () => {
                          const seatButtons = document.querySelectorAll('button[data-seatlocno]');
                          const visibleSeats = Array.from(seatButtons).filter(b => b.offsetParent !== null && !b.hidden);
                          const bodyText = document.body.innerText || '';
                          const hasPaySection = bodyText.includes('결제수단') ||
                                                bodyText.includes('N pay') ||
                                                bodyText.includes('최종 결제금액') ||
                                                bodyText.includes('결제하기');
                          return (seatButtons.length === 0 || visibleSeats.length === 0) || hasPaySection;
                        }
                        """
                    )
                )
            except Exception:
                return False

        start_time = time.monotonic()
        while (
            not self.stop_event.is_set()
            and time.monotonic() - start_time
            < self.SEAT_SUBMIT_TRANSITION_TIMEOUT_SECONDS
        ):
            if check_conflict():
                self._click_visible_by_text(page, ("확인", "닫기", "취소"))
                return False

            if check_transition():
                return True

            try:
                page.wait_for_timeout(150)
            except Exception:
                if self.stop_event.wait(0.15):
                    break

        if check_conflict():
            self._click_visible_by_text(page, ("확인", "닫기", "취소"))
            return False

        return check_transition()

    def _reconnect_seat_session(
        self,
        schedule: dict[str, Any] | None = None,
        people: int = 1,
    ) -> Any:
        try:
            playwright = getattr(self, "_playwright", None)
            chrome = getattr(self, "_chrome", None)
            browser = getattr(self, "_browser", None)
            if chrome is None or not getattr(chrome, "endpoint", None):
                chrome = browser_session.start_isolated(log=self.log)
                self._chrome = chrome
            if browser is None or not getattr(browser, "is_connected", lambda: True)():
                if playwright is not None and chrome is not None and getattr(chrome, "endpoint", None):
                    browser = playwright.chromium.connect_over_cdp(chrome.endpoint)
                    self._browser = browser
            context = None
            if browser is not None:
                try:
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                except Exception:
                    if playwright is not None and chrome is not None and getattr(chrome, "endpoint", None):
                        browser = playwright.chromium.connect_over_cdp(chrome.endpoint)
                        self._browser = browser
                        context = browser.contexts[0] if browser.contexts else browser.new_context()
                self._context = context
            if context is not None:
                page = next(
                    (
                        item
                        for item in context.pages
                        if not item.is_closed() and "cgv.co.kr" in item.url
                    ),
                    None,
                )
                page = page or context.new_page()
                page.on("dialog", lambda dialog: dialog.accept())
                if schedule:
                    if not self._enter_visitor_page(page, schedule):
                        return None
                else:
                    page.goto(
                        f"{CGV_HOME_URL}/cnm/selectVisitorCnt",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                if not self._select_visitors(page, people):
                    return None
                return page
        except Exception as rec_err:
            self.log(
                f"[CGV] 좌석 단계 브라우저 재연결 대기/실패: {format_exception(rec_err)}",
                "warning",
            )
        return None

    def _reload_or_recover_seat_page(
        self,
        page,
        schedule: dict[str, Any] | None = None,
        people: int = 1,
    ) -> tuple[Any, bool]:
        """Safely reload seat page; if TargetClosedError or CDP disconnect occurs, reconnect and restore state."""
        try:
            if hasattr(page, "is_closed") and page.is_closed():
                raise RuntimeError("TargetClosedError: Target page has been closed")
            page.reload(wait_until="domcontentloaded", timeout=30000)
            if self._is_block_page(page):
                return page, False
            if not self._select_visitors(page, people):
                return page, False
            return page, True
        except Exception as exc:
            if self.stop_event.is_set():
                return page, False
            if self._is_recoverable_browser_error(exc) or (
                hasattr(page, "is_closed") and page.is_closed()
            ):
                self.log(
                    f"[CGV] 좌석 단계 브라우저 연결 끊김 감지 ({exc.__class__.__name__}) · 자동 재연결 시도 중...",
                    "warning",
                )
                recovered_page = self._reconnect_seat_session(schedule, people)
                if recovered_page is not None:
                    self.log("[CGV] 좌석 단계 브라우저 재연결 성공 · 좌석 선택을 계속합니다.", "success")
                    return recovered_page, True
            self.log(f"CGV 좌석 페이지 새로고침 오류: {format_exception(exc)}", "warning")
            return page, False

    def _prepare_browser_fallback_page(
        self,
        page,
        *,
        schedule: dict[str, Any] | None = None,
        people: int = 1,
        fallback_reason: str = "",
    ) -> tuple[Any, bool]:
        """Keep a usable seat DOM instead of blindly reloading after fast-path failure."""

        seat_state = self._seat_modal_snapshot(page)
        if int(seat_state.get("seatCount", 0) or 0) > 0:
            self.log(
                "CGV 고속 감시 종료 · 이미 열린 좌석 화면을 유지하고 선택을 계속합니다.",
                "info",
            )
            return page, True

        if fallback_reason in {"rate-limited", "access-forbidden"}:
            cooldown = max(
                self.RATE_LIMIT_BROWSER_RELOAD_INTERVAL,
                self._last_fast_retry_after_seconds,
            )
            self.log(
                f"CGV 좌석 API 제한 해제를 {cooldown:.0f}초 기다린 뒤 "
                "브라우저 좌석 화면을 복구합니다.",
                "warning",
            )
            if self.stop_event.wait(cooldown):
                return page, False

        return self._reload_or_recover_seat_page(
            page,
            schedule=schedule,
            people=people,
        )

    def _select_and_hold_seats(
        self,
        page,
        groups: tuple[CgvSeatGroup, ...],
        people: int,
        developer_mode: bool,
        schedule: dict[str, Any] | None = None,
        fallback_reason: str = "",
    ) -> bool:
        last_reload = time.monotonic()
        rate_limited = fallback_reason in {"rate-limited", "access-forbidden"}
        reload_interval = (
            self.RATE_LIMIT_BROWSER_RELOAD_INTERVAL
            if rate_limited
            else self.BROWSER_SEAT_RELOAD_INTERVAL
        )
        while not self.stop_event.is_set():
            if self._is_block_page(page):
                if not rate_limited:
                    self.log("CGV 접근 제한이 감지되어 좌석 조회를 중지했습니다.", "error")
                    return False
                self.silent_tick(
                    f"CGV 접근 제한 해제 대기 · {reload_interval:.1f}초 후 좌석 화면 복구"
                )
                if self.stop_event.wait(reload_interval):
                    return False
                page, ok = self._reload_or_recover_seat_page(
                    page, schedule=schedule, people=people
                )
                if ok:
                    reload_interval = self.RATE_LIMIT_BROWSER_RELOAD_INTERVAL
                else:
                    reload_interval = min(
                        self.RATE_LIMIT_BROWSER_MAX_RELOAD_INTERVAL,
                        max(
                            self.RATE_LIMIT_BROWSER_RELOAD_INTERVAL,
                            reload_interval * 2,
                        ),
                    )
                last_reload = time.monotonic()
                continue
            elements = self._available_seat_elements(page)
            chosen = self.choose_available_group(elements, groups)
            if chosen:
                group, ids = chosen
                selection_ok = True
                for seat in group.seats:
                    if not self._ensure_seat_selected_by_id(page, ids[seat]):
                        self.silent_tick(f"CGV 좌석 {seat} 선택 실패 · 다시 시도")
                        selection_ok = False
                        break
                if not selection_ok:
                    page, ok = self._reload_or_recover_seat_page(
                        page, schedule=schedule, people=people
                    )
                    if not ok:
                        return False
                    last_reload = time.monotonic()
                    continue

                self.log(f"선택 좌석 확보 가능: {', '.join(group.seats)}", "success")
                selected_ids = [str(ids[seat]) for seat in group.seats]
                if not self._wait_for_seat_selection_ready(page, selected_ids):
                    self.silent_tick("CGV 좌석 선택 화면 반영 지연 · 다시 시도")
                    page, ok = self._reload_or_recover_seat_page(
                        page, schedule=schedule, people=people
                    )
                    if not ok:
                        return False
                    last_reload = time.monotonic()
                    continue
                if not self._submit_seat_selection(page):
                    self.silent_tick("CGV 좌석 선점 경합 발생 또는 모달 전환 재시도")
                    page, ok = self._reload_or_recover_seat_page(
                        page, schedule=schedule, people=people
                    )
                    if not ok:
                        return False
                    last_reload = time.monotonic()
                    continue

                self.log("CGV 좌석 임시선점 완료 · 결제 페이지로 이동했습니다.", "success")
                return True

            self.silent_tick("선택한 CGV 좌석 묶음이 아직 비어 있지 않습니다")
            now = time.monotonic()
            if now - last_reload >= reload_interval:
                page, ok = self._reload_or_recover_seat_page(
                    page, schedule=schedule, people=people
                )
                if not ok:
                    if not rate_limited or self.stop_event.is_set():
                        return False
                    reload_interval = min(
                        self.RATE_LIMIT_BROWSER_MAX_RELOAD_INTERVAL,
                        max(
                            self.RATE_LIMIT_BROWSER_RELOAD_INTERVAL,
                            reload_interval * 2,
                        ),
                    )
                    self.silent_tick(
                        f"CGV 좌석 데이터 복구 대기 · {reload_interval:.1f}초 후 재시도"
                    )
                elif rate_limited:
                    reload_interval = self.RATE_LIMIT_BROWSER_RELOAD_INTERVAL
                last_reload = time.monotonic()
            else:
                try:
                    page.wait_for_timeout(300)
                except Exception:
                    self.stop_event.wait(0.3)
        return False

    def _ensure_member_session(self, page, context) -> bool:
        def has_session() -> bool:
            try:
                return any(
                    cookie.get("name") in {"accessToken", "refresh_token"}
                    and cookie.get("value")
                    for cookie in context.cookies(CGV_HOME_URL)
                )
            except Exception:
                return False

        # Navigate to login page to verify if session is genuinely active or login is needed
        page.goto(
            f"{CGV_HOME_URL}/mem/login?nmbrAtktFlag=Y",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.wait_for_timeout(800)

        # If already actively logged in, CGV immediately redirects away from /mem/login
        if "/mem/login" not in page.url and has_session():
            self.log("CGV 회원 로그인이 확인되었습니다.", "success")
            return True

        self.log(
            "회원 예매를 위해 열린 CGV Chrome에서 로그인해주세요. 로그인 완료 후 자동으로 예약을 시작합니다.",
            "warning",
        )
        while not self.stop_event.is_set():
            if page.is_closed():
                return False
            # When user completes login, CGV redirects away from /mem/login
            if "/mem/login" not in page.url and has_session():
                self.log("CGV 회원 로그인을 확인했습니다. 예약을 시작합니다.", "success")
                return True
            page.wait_for_timeout(500)
        return False

    def _prepare_nonmember_session(self, page, cgv: dict[str, Any]) -> bool:
        birth = re.sub(r"\D", "", str(cgv.get("nonmember_birth", "")))
        phone = re.sub(r"\D", "", str(cgv.get("nonmember_phone", "")))
        password = str(cgv.get("nonmember_password", ""))
        page.goto(
            f"{CGV_HOME_URL}/mem/nmbrAtkt/crtf",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.locator("#bymd").fill(birth)
        page.locator("#pwd").fill(password)
        page.locator("#chkPwd").fill(password)
        page.locator("#mbltNo").fill(phone)
        request_button = page.get_by_text("인증요청", exact=True)
        for index in range(request_button.count()):
            button = request_button.nth(index)
            if button.is_visible() and button.is_enabled():
                button.click(timeout=2500)
                break
        self.log(
            "CGV 비회원 인증 문자를 요청했습니다. 열린 Chrome에 인증번호를 입력하면 자동으로 확인합니다.",
            "warning",
        )
        submitted = False
        while not self.stop_event.is_set():
            if "/mem/nmbrAtkt/crtf" not in page.url:
                self.log("CGV 비회원 인증을 확인했습니다.", "success")
                return True
            try:
                code = re.sub(r"\D", "", page.locator("#crtfNo").input_value(timeout=1000))
            except Exception:
                code = ""
            if len(code) >= 4 and not submitted:
                buttons = page.get_by_text("인증확인", exact=True)
                for index in range(buttons.count()):
                    button = buttons.nth(index)
                    if button.is_visible() and button.is_enabled():
                        button.click(timeout=2500)
                        submitted = True
                        break
            try:
                body = page.locator("body").inner_text(timeout=1000)
            except Exception:
                body = ""
            if submitted and any(
                phrase in body for phrase in ("인증이 완료", "인증되었습니다", "비회원 인증 완료")
            ):
                self.log("CGV 비회원 인증을 확인했습니다.", "success")
                return True
            page.wait_for_timeout(500)
        return False

    def _prepare_authentication(self, page, context, cgv: dict[str, Any]) -> bool:
        if str(cgv.get("booking_mode", "회원")) == "비회원":
            return self._prepare_nonmember_session(page, cgv)
        return self._ensure_member_session(page, context)

    def _wait_schedule_cycle(self, started: float, interval: float) -> None:
        # No catch-up burst after a slow request, and no overlapping cycles.
        self.stop_event.wait(max(0.05, float(interval) - (time.monotonic() - started)))

    def _note_schedule_observation(self, started: float, opened: bool) -> None:
        now = time.monotonic()
        if not opened:
            self._cgv_last_closed_poll = (started, now)
            return
        self._cgv_open_detected_at = now
        prior = getattr(self, "_cgv_last_closed_poll", None)
        detail = (f" · 마지막 미오픈 조회 시작부터 {(now-prior[0])*1000:.0f}ms"
                  if prior else " · 이전 미오픈 표본 없음")
        self.log(f"[CGV 속도] 첫 회차 확인 · 이번 조회·처리 {(now-started)*1000:.0f}ms"
                 f"{detail} · 서버 실제 오픈 시각과는 구분", "info")

    def make_reservation_thread(self, reservation_data: dict[str, Any]) -> None:
        from playwright.sync_api import sync_playwright

        metadata = reservation_data.get("engine_metadata", {})
        cgv = metadata.get("cgv", {}) if isinstance(metadata, dict) else {}
        self._developer_hold_cleanup = None
        site_no = str(reservation_data.get("branch", "")).strip()
        movie = str(cgv.get("movie") or reservation_data.get("themePK", "")).strip()
        auditorium = str(cgv.get("auditorium", "")).strip()
        format_name = str(cgv.get("format", "")).strip()
        show_time = str(reservation_data.get("reservationTime", ""))
        preferred_times = list(cgv.get("preferred_times") or ([show_time] if show_time else []))
        screening_date = str(reservation_data.get("reservationDate", ""))
        site_name = str(cgv.get("site_name") or reservation_data.get("branchLabel", "CGV"))
        people = max(1, int(reservation_data.get("people", 1)))
        groups = parse_seat_groups(str(cgv.get("seats", "")), people)
        if not groups:
            self.log("CGV 좌석 우선순위를 해석하지 못했습니다.", "error")
            return

        chrome = browser_session.start_isolated(log=self.log)
        if chrome is None:
            self.log("CGV용 Chrome 세션을 시작하지 못했습니다.", "error")
            return
        keep_open = False
        try:
            with self._browser_lock, sync_playwright() as playwright:
                self._playwright = playwright
                self._chrome = chrome
                browser = playwright.chromium.connect_over_cdp(chrome.endpoint)
                self._browser = browser
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                self._context = context
                page = next((item for item in context.pages if "cgv.co.kr" in item.url), None)
                page = page or context.new_page()
                page.on("dialog", lambda dialog: dialog.accept())
                if not self._prepare_authentication(page, context, cgv):
                    keep_open = True
                    return
                page.goto(
                    self._cinema_url(site_no, site_name),
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                if self._is_block_page(page):
                    self.log(
                        "CGV가 현재 네트워크 접근을 제한했습니다. 잠시 뒤 다시 시도해주세요.",
                        "error",
                    )
                    keep_open = True
                    return

                schedule_url = self._schedule_url(site_no, screening_date)
                concurrency = self.scan_concurrency
                current_state = ""
                schedule = None
                error_backoff = 1.0
                self._cgv_last_closed_poll = None
                self._cgv_open_detected_at = None
                while not self.stop_event.is_set():
                    poll_started = time.monotonic()
                    try:
                        result = self._race_schedule(page, schedule_url, concurrency)
                        if result.pop("_pengucroResetScheduleBackoff", False):
                            error_backoff = 1.0
                    except Exception as exc:
                        if self.stop_event.is_set():
                            break
                        if self._is_recoverable_browser_error(exc) or (hasattr(page, "is_closed") and page.is_closed()):
                            self.log(
                                f"[CGV] 브라우저 연결 끊김 감지 ({exc.__class__.__name__}) · 자동 재연결 시도 중...",
                                "warning",
                            )
                            try:
                                if not browser.is_connected():
                                    chrome = browser_session.start_isolated(log=self.log) or chrome
                                    browser = playwright.chromium.connect_over_cdp(chrome.endpoint)
                                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                                page = next(
                                    (item for item in context.pages if not item.is_closed() and "cgv.co.kr" in item.url),
                                    None,
                                )
                                page = page or context.new_page()
                                page.on("dialog", lambda dialog: dialog.accept())
                                page.goto(
                                    self._cinema_url(site_no, site_name),
                                    wait_until="domcontentloaded",
                                    timeout=30000,
                                )
                                self.log("[CGV] 브라우저 재연결 성공 · 미오픈 감시를 계속합니다.", "success")
                                continue
                            except Exception as rec_err:
                                self.log(
                                    f"[CGV] 브라우저 재연결 대기 중... ({format_exception(rec_err)})",
                                    "warning",
                                )
                                self.stop_event.wait(3.0)
                                continue
                        raise
                    status = int(result.get("status", 0) or 0)
                    elapsed = float(result.get("elapsedMs", 0.0) or 0.0)
                    if result.get("ok"):
                        error_backoff = 1.0
                        payload_data = result.get("data", {})
                        schedule = select_schedule(
                            payload_data,
                            movie=movie,
                            show_time=show_time,
                            auditorium=auditorium,
                            preferred_times=preferred_times,
                            format_name=format_name,
                        )
                        self._note_schedule_observation(poll_started, bool(schedule))
                        if schedule:
                            sched_time = normalize_time(schedule.get("scnsrtTm"))
                            time_label = (
                                f"{sched_time[:2]}:{sched_time[2:]}"
                                if len(sched_time) == 4
                                else (show_time[:5] if show_time else "")
                            )
                            self.log(
                                f"[CGV] 실제 IMAX 회차 감지 · {movie} · {time_label} · {elapsed:.0f}ms · 고속 선점 모드 전환",
                                "success",
                            )
                            self.log(
                                "[CGV] 브라우저 화면 준비를 기다리지 않고 좌석 API 선점을 즉시 시작합니다.",
                                "info",
                            )
                            break

                        has_hint = _has_schedule_hint(payload_data, movie, auditorium)
                        if has_hint:
                            if current_state != "SCHEDULE_HINT":
                                current_state = "SCHEDULE_HINT"
                                self.log("[CGV] 목표 영화 선공개 감지 · 감시 간격 단축 (2초)", "warning")
                            self.silent_tick("목표 영화/상영관 선공개 감지 · 오픈 대기 중")
                            poll_interval = self.SCHEDULE_HINT_INTERVAL
                        else:
                            if current_state != "PREOPEN_IDLE":
                                current_state = "PREOPEN_IDLE"
                                self.log("[CGV] 미오픈 대기 · 20초 간격으로 시간표 확인", "info")
                            self.silent_tick("선택한 CGV 회차가 아직 열리지 않았습니다 (미오픈 대기)")
                            poll_interval = self.PREOPEN_IDLE_INTERVAL

                        if elapsed > 2500 and concurrency > 1:
                            concurrency -= 1
                            self.log(
                                f"CGV 응답 지연이 커 동시 조회를 {concurrency}개로 자동 감속합니다.",
                                "warning",
                            )
                        self._wait_schedule_cycle(poll_started, poll_interval)
                    else:
                        if status in {403, 429} or any(
                            int(value or 0) in {403, 429} for value in result.get("statuses", [])
                        ):
                            if concurrency > 1:
                                concurrency = 1
                            error_backoff = min(self.MAX_BACKOFF, max(3.0, error_backoff * 2))
                            self.silent_tick(
                                f"CGV 연결 제한({status or '응답 없음'}) · {error_backoff:.1f}초 후 재시도"
                            )
                        else:
                            error_backoff = min(self.MAX_BACKOFF, max(1.0, error_backoff * 1.5))
                            self.silent_tick("CGV 회차 조회 통신 오류")
                        self.stop_event.wait(error_backoff)

                if self.stop_event.is_set() or not schedule:
                    return
                developer_mode = parse_bool_flag(reservation_data.get("devMode", False))
                held, use_browser_fallback = self._watch_and_hold_api(
                    page,
                    schedule,
                    groups,
                    people,
                    developer_mode,
                    cgv,
                )
                if use_browser_fallback and not self.stop_event.is_set():
                    self._restore_fetch(page)
                    fallback_reason = self._last_fast_monitor_exit_reason
                    page, ok = self._prepare_browser_fallback_page(
                        page,
                        schedule=schedule,
                        people=people,
                        fallback_reason=fallback_reason,
                    )
                    if ok:
                        held = self._select_and_hold_seats(
                            page,
                            groups,
                            people,
                            developer_mode,
                            schedule=schedule,
                            fallback_reason=fallback_reason,
                        )
                keep_open = True
                if held:
                    npay_password = str(cgv.get("npay_password", "")).strip()
                    self._finish_held_checkout(
                        page,
                        developer_mode=developer_mode,
                        npay_password=npay_password,
                        site_no=site_no,
                        movie=movie,
                    )
        except Exception as exc:
            if not self.stop_event.is_set():
                self.log(f"CGV 예약 흐름 오류: {format_exception(exc)}", "error")
                keep_open = True
        finally:
            self._playwright = None
            self._browser = None
            self._context = None
            self._chrome = None
            if keep_open:
                self._release_browser_lease_when_closed(chrome)
            else:
                chrome.close_if_launched()
                chrome.release()
