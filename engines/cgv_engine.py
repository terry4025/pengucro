from __future__ import annotations

import re
import threading
import time
import urllib.parse
from typing import Any

from engines import browser_session
from engines.base_engine import BaseEngine
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
from pengucro.diagnostics import format_exception
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
    HEDGE_DELAY_MS = 110
    MAX_BACKOFF = 15.0
    BROWSER_SEAT_RELOAD_INTERVAL = 1.5
    RATE_LIMIT_BROWSER_RELOAD_INTERVAL = 4.0
    RATE_LIMIT_BROWSER_MAX_RELOAD_INTERVAL = 15.0
    VISITOR_SELECTION_TIMEOUT = 12.0
    VISITOR_RETRY_INTERVAL_MS = 350

    def __init__(self, log_callback, success_callback=None, **kwargs) -> None:
        super().__init__(log_callback, success_callback, **kwargs)
        self.scan_concurrency = 1
        self._browser_lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._chrome = None
        self._last_fast_monitor_exit_reason = ""

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
            f"{self.FAST_SEAT_LAUNCH_INTERVAL_MS}ms 간격으로 감시하며 "
            "제한 신호가 감지되면 자동으로 감속합니다.",
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
          return await new Promise((resolve) => {
            let failed = 0;
            const statuses = [];
            const timers = [];
            let settled = false;
            const launch = (controller, index) => {
              if (settled) return;
              fetch(url, {
                method: 'GET', cache: 'no-store', credentials: 'include',
                headers: {'Accept': 'application/json, text/plain, */*'},
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

    @staticmethod
    def _post_json(page, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = page.evaluate(
            """
            async ({url, payload}) => {
              try {
                const response = await fetch(url, {
                  method: 'POST', credentials: 'include', cache: 'no-store',
                  headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
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

    def _select_api_seats_in_ui(self, page, payload, selected) -> bool:
        self._sync_seat_payload_to_ui(page, payload)
        for seat in selected:
            seat_id = getattr(seat, "seat_id", None) or (
                seat.get("seat_id") or seat.get("seatLocNo") or seat.get("id")
                if isinstance(seat, dict)
                else str(seat)
            )
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
        return True

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

    def _cancel_api_hold(self, page, hold_payload, hold_response) -> None:
        data = hold_response.get("data", {}) if isinstance(hold_response, dict) else {}
        mov_atkt_no = str(data.get("movAtktNo", "")) if isinstance(data, dict) else ""
        if not mov_atkt_no:
            return
        cancel_payload = {
            "coCd": hold_payload.get("coCd", CGV_COMPANY_CODE),
            "movAtktNo": mov_atkt_no,
            "sachlTypCd": hold_payload.get("sachlTypCd", "01"),
            "rtctlScopCd": hold_payload.get("rtctlScopCd", "08"),
            "custNo": hold_payload.get("custNo", ""),
            "seatPrmpDataList": hold_payload.get("seatPrmpDataList", []),
        }
        self._post_json(
            page, f"{CGV_BFF_CONTENT_URL}/seatTemp/seatTempPrmpCncl", cancel_payload
        )

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
    ) -> bool:
        """Start a same-origin browser monitor with staggered persistent GETs."""

        interval = max(
            80,
            int(launch_interval_ms or self.FAST_SEAT_LAUNCH_INTERVAL_MS),
        )
        try:
            result = page.evaluate(
                r"""
                ({url, groups, concurrency, intervalMs, maxConsecutiveErrors, directHold}) => {
                  const previous = window.__pengucroFastSeatMonitor;
                  if (previous && typeof previous.stop === 'function') previous.stop();

                  const state = {
                    running: true,
                    attempts: 0,
                    completed: 0,
                    inflight: 0,
                    consecutiveErrors: 0,
                    lastStatus: 0,
                    lastElapsedMs: 0,
                    blocked: false,
                    unauthorized: false,
                    lastError: '',
                    terminalError: '',
                    conflicts: 0,
                    hit: null,
                    timer: null,
                    controllers: new Set(),
                  };
                  const normalize = value => String(value || '')
                    .toUpperCase().replace(/[\s_-]+/g, '');
                  const normalizedGroups = groups.map(group => group.map(normalize));
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
                    if (!state.running && !state.timer) return;
                    state.running = false;
                    if (state.timer) clearInterval(state.timer);
                    state.timer = null;
                    for (const controller of state.controllers) controller.abort();
                    state.controllers.clear();
                  };
                  const pauseOtherRequests = keep => {
                    state.running = false;
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
                      headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
                      body: JSON.stringify(body),
                      signal,
                    });
                    let data = null;
                    try { data = await response.json(); } catch (_) {}
                    return {ok: response.ok, status: response.status, data};
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
                      zoneGroupYn: 'N',
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
                  let launch;
                  const resume = () => {
                    if (state.hit || state.blocked || state.unauthorized || state.terminalError) return;
                    state.running = true;
                    state.timer = setInterval(launch, intervalMs);
                    setTimeout(launch, 0);
                  };
                  launch = async () => {
                    if (!state.running || state.inflight >= concurrency) return;
                    const controller = new AbortController();
                    state.controllers.add(controller);
                    state.inflight += 1;
                    state.attempts += 1;
                    const started = performance.now();
                    try {
                      const response = await fetch(url, {
                        method: 'GET',
                        cache: 'no-store',
                        credentials: 'include',
                        headers: {'Accept': 'application/json, text/plain, */*'},
                        signal: controller.signal,
                      });
                      state.lastStatus = response.status;
                      state.lastElapsedMs = performance.now() - started;
                      if (response.status === 403 || response.status === 429) {
                        state.blocked = true;
                        state.lastError = `HTTP ${response.status}`;
                        state.stop();
                        return;
                      }
                      if (response.status === 401) {
                        state.unauthorized = true;
                        state.lastError = 'HTTP 401';
                        state.stop();
                        return;
                      }
                      if (!response.ok) throw new Error(`HTTP ${response.status}`);
                      const payload = await response.json();
                      state.consecutiveErrors = 0;
                      const group = findGroup(payload);
                      if (group) {
                        pauseOtherRequests(controller);
                        if (!directHold) {
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
                        try {
                          const pricePayload = buildPricePayload(group.seats);
                          const price = await postJson(
                            directHold.priceUrl,
                            pricePayload,
                            transactionController.signal,
                          );
                          state.lastStatus = price.status;
                          if (price.status === 401 || price.status === 403) {
                            state.unauthorized = true;
                            state.lastError = `price HTTP ${price.status}`;
                            return;
                          }
                          if (price.status === 429) {
                            state.blocked = true;
                            state.lastError = 'price HTTP 429';
                            return;
                          }
                          if (!price.data || typeof price.data !== 'object') {
                            state.terminalError = 'price-response-shape';
                            return;
                          }
                          if (!price.ok || Number(price.data.statusCode ?? -1) !== 0) {
                            state.conflicts += 1;
                            resume();
                            return;
                          }

                          const holdPayload = buildHoldPayload(group.seats);
                          const hold = await postJson(
                            directHold.holdUrl,
                            holdPayload,
                            transactionController.signal,
                          );
                          state.lastStatus = hold.status;
                          if (hold.status === 401 || hold.status === 403) {
                            state.unauthorized = true;
                            state.lastError = `hold HTTP ${hold.status}`;
                            return;
                          }
                          if (hold.status === 429) {
                            state.blocked = true;
                            state.lastError = 'hold HTTP 429';
                            return;
                          }
                          if (!hold.data || typeof hold.data !== 'object') {
                            state.terminalError = 'hold-response-shape';
                            return;
                          }
                          const holdData = hold.data.data || {};
                          const held = hold.ok
                            && Number(hold.data.statusCode ?? -1) === 0
                            && String(holdData.resultCode ?? '0') === '0'
                            && Boolean(holdData.movAtktNo);
                          if (!held) {
                            state.conflicts += 1;
                            resume();
                            return;
                          }
                          state.hit = {
                            data: payload,
                            group: group.labels,
                            elapsedMs: state.lastElapsedMs,
                            transaction: {
                              priceResponse: price.data,
                              holdResponse: hold.data,
                              holdPayload,
                              elapsedMs: performance.now() - transactionStarted,
                            },
                          };
                        } finally {
                          state.controllers.delete(transactionController);
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
                  launch();
                  state.timer = setInterval(launch, intervalMs);
                  return true;
                }
                """,
                {
                    "url": seat_url,
                    "groups": [list(group.seats) for group in groups],
                    "concurrency": max(1, min(int(concurrency), CGV_MAX_WORKERS)),
                    "intervalMs": interval,
                    "maxConsecutiveErrors": self.FAST_MONITOR_MAX_CONSECUTIVE_ERRORS,
                    "directHold": direct_hold,
                },
            )
            return bool(result)
        except Exception:
            return False

    @staticmethod
    def _read_fast_seat_monitor(page) -> dict[str, Any]:
        try:
            result = page.evaluate(
                r"""
                () => {
                  const state = window.__pengucroFastSeatMonitor;
                  if (!state) return null;
                  return {
                    running: state.running,
                    attempts: state.attempts,
                    completed: state.completed,
                    inflight: state.inflight,
                    consecutiveErrors: state.consecutiveErrors,
                    lastStatus: state.lastStatus,
                    lastElapsedMs: state.lastElapsedMs,
                    blocked: state.blocked,
                    unauthorized: state.unauthorized,
                    lastError: state.lastError,
                    terminalError: state.terminalError,
                    conflicts: state.conflicts,
                    hit: state.hit,
                  };
                }
                """
            )
            return dict(result) if isinstance(result, dict) else {}
        except Exception:
            return {}

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
        auth = self._browser_auth_data(page)
        seat_url = self._seat_url(schedule, auth.get("custNo", ""))
        direct_hold = self._direct_hold_config(schedule, people, auth, cgv)
        preferred_concurrency = max(
            1,
            min(self.scan_concurrency, int(self.FAST_SEAT_MAX_INFLIGHT)),
        )
        concurrency = preferred_concurrency
        launch_interval_ms = self.FAST_SEAT_LAUNCH_INTERVAL_MS
        backoff = self.MIN_POLL_INTERVAL
        while not self.stop_event.is_set():
            started = self._start_fast_seat_monitor(
                page,
                seat_url,
                groups,
                concurrency,
                launch_interval_ms=launch_interval_ms,
                direct_hold=direct_hold,
            )
            if not started:
                self._last_fast_monitor_exit_reason = "monitor-start-failed"
                self.log("CGV 고속 좌석 감시기를 시작하지 못해 안전 경로로 전환합니다.", "warning")
                return False, True

            last_completed = 0
            snapshot: dict[str, Any] = {}
            try:
                while not self.stop_event.wait(self.FAST_MONITOR_READ_INTERVAL):
                    snapshot = self._read_fast_seat_monitor(page)
                    if not snapshot:
                        break
                    completed = max(0, int(snapshot.get("completed", 0) or 0))
                    if completed > last_completed:
                        self.silent_ticks(
                            completed - last_completed,
                            "선택한 CGV 좌석 묶음을 고속 API로 감시 중",
                        )
                        last_completed = completed
                    if snapshot.get("hit") or not snapshot.get("running", False):
                        break
            finally:
                self._stop_fast_seat_monitor(page)

            if self.stop_event.is_set():
                return False, False

            hit = snapshot.get("hit") if isinstance(snapshot, dict) else None
            if not isinstance(hit, dict):
                status = int(snapshot.get("lastStatus", 0) or 0)
                if snapshot.get("unauthorized") or status == 401:
                    self._last_fast_monitor_exit_reason = "unauthorized"
                    self.log("CGV 로그인 인증이 만료되어 브라우저 확인 경로로 전환합니다.", "warning")
                    return False, True
                if snapshot.get("terminalError"):
                    self._last_fast_monitor_exit_reason = str(
                        snapshot.get("terminalError") or "terminal-error"
                    )
                    self.log("CGV 선점 API 응답 구조가 변경되어 브라우저 안전 경로로 전환합니다.", "warning")
                    return False, True
                if snapshot.get("blocked") or status in {403, 429}:
                    concurrency = 1
                    launch_interval_ms = min(1000, max(400, launch_interval_ms * 2))
                    backoff = min(self.MAX_BACKOFF, max(3.0, backoff * 2))
                    self.silent_tick(f"CGV 좌석 API 연결 제한 · {backoff:.1f}초 후 재시도")
                else:
                    backoff = min(self.MAX_BACKOFF, max(0.5, backoff * 1.5))
                    self.silent_tick("CGV 고속 좌석 API 조회 오류")
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
            if not self._select_api_seats_in_ui(page, seat_payload, selected):
                self._cancel_api_hold(page, hold_payload, hold_response)
                self._last_fast_monitor_exit_reason = "ui-sync-failed"
                self.log("CGV 화면 동기화에 실패해 확보 좌석을 해제하고 안전 경로로 전환합니다.", "warning")
                return False, True

            self._install_cached_hold_responses(page, price_response, hold_response)
            if self._click_visible_by_text(page, ("선택완료", "선택 완료")):
                try:
                    page.get_by_text("결제하기", exact=True).last.wait_for(
                        state="visible", timeout=15000
                    )
                    self._restore_fetch(page)
                    return True, False
                except Exception:
                    pass
            self._restore_fetch(page)
            self._cancel_api_hold(page, hold_payload, hold_response)
            self._last_fast_monitor_exit_reason = "checkout-transition-failed"
            self.log(
                "CGV 결제 화면 연결이 확인되지 않아 임시선점을 취소하고 브라우저 안전 경로로 전환합니다.",
                "warning",
            )
            return False, True
        return False, False

    def _proceed_naver_pay_checkout(self, page, developer_mode: bool = False) -> bool:
        """Automate CGV checkout using Naver Pay (N pay).

        1. Select 'N pay' payment method.
        2. Check mandatory terms agreement.
        3. Click CGV '결제하기' to launch Naver Pay popup.
        4. In the Naver Pay popup:
           - In developer mode: keep popup open and skip clicking '동의하고 결제하기'.
           - In normal mode: click '동의하고 결제하기' to finalize payment.
        """
        try:
            self.log("[CGV] 결제 페이지 진입 · 네이버페이 자동 결제 진행 중...", "info")
            page.wait_for_timeout(1000)

            # 1. Click N pay button
            npay_candidates = [
                page.locator("button, div[role='button'], a, div, label, span").filter(has_text="N pay"),
                page.locator("button, div[role='button'], a, div, label, span").filter(has_text="npay"),
                page.locator("img[alt*='npay'], img[alt*='N pay'], img[alt*='네이버페이']"),
                page.locator("button:has-text('N pay'), button:has-text('npay')"),
            ]
            clicked_npay = False
            for loc in npay_candidates:
                try:
                    for idx in range(loc.count()):
                        cand = loc.nth(idx)
                        if cand.is_visible():
                            cand.click(force=True, timeout=2000)
                            clicked_npay = True
                            break
                    if clicked_npay:
                        break
                except Exception:
                    continue

            # JavaScript fallback for N pay selection
            if not clicked_npay:
                try:
                    clicked_npay = page.evaluate(
                        r"""
                        () => {
                          const clean = s => (s || '').replace(/\s+/g, '').toLowerCase();
                          const elements = Array.from(document.querySelectorAll('button, div[role="button"], a, label, span, div, p'));
                          const target = elements.find(el => clean(el.textContent).includes('npay') || clean(el.textContent).includes('네이버페이'));
                          if (target) {
                            (target.closest('button, label, div[role="button"], a') || target).click();
                            return true;
                          }
                          return false;
                        }
                        """
                    )
                except Exception:
                    clicked_npay = False

            if clicked_npay:
                self.log("[CGV] 결제수단 N pay 선택 완료", "info")
            else:
                self.log("CGV 결제수단에서 N pay 버튼을 찾지 못했습니다. 수동으로 선택해주세요.", "warning")

            page.wait_for_timeout(500)

            # 2. Check terms agreement (약관 전체 동의)
            terms_candidates = [
                page.locator("label:has-text('전체 동의'), label:has-text('약관 전체 동의'), label:has-text('모두 동의')").first,
                page.locator("label:has-text('동의')").last,
                page.locator("input[type='checkbox']").last,
            ]
            for loc in terms_candidates:
                try:
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(timeout=2000)
                        break
                except Exception:
                    continue

            try:
                page.evaluate(
                    r"""
                    () => {
                      const clean = s => (s || '').replace(/\s+/g, '');
                      const labels = Array.from(document.querySelectorAll('label, span, div, p'));
                      const target = labels.find(el => clean(el.textContent).includes('전체동의') || clean(el.textContent).includes('약관전체동의') || clean(el.textContent).includes('모두동의'));
                      if (target) {
                        (target.closest('label') || target).click();
                        return true;
                      }
                      return false;
                    }
                    """
                )
            except Exception:
                pass

            page.wait_for_timeout(500)

            # 3. Click CGV '결제하기' button and capture Naver Pay popup
            pay_btn = page.get_by_text("결제하기", exact=True).last
            if not pay_btn.is_visible():
                pay_btn = page.locator("button:has-text('결제하기'), a:has-text('결제하기')").last

            self.log("[CGV] 네이버페이 결제창 호출 중...", "info")
            naver_pay_page = None
            try:
                with page.context.expect_page(timeout=15000) as popup_info:
                    if pay_btn.is_visible():
                        pay_btn.click(force=True, timeout=5000)
                    else:
                        page.evaluate(
                            r"""
                            () => {
                              const clean = s => (s || '').replace(/\s+/g, '');
                              const buttons = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
                              const target = buttons.reverse().find(b => clean(b.textContent) === '결제하기' && !b.disabled);
                              if (target) target.click();
                            }
                            """
                        )
                naver_pay_page = popup_info.value
            except Exception:
                for p in page.context.pages:
                    if p != page and not p.is_closed() and any(
                        domain in p.url for domain in ("naver.com", "pstatic.net", "instantPay")
                    ):
                        naver_pay_page = p
                        break

            if naver_pay_page is None:
                self.log("네이버페이 결제창을 감지하지 못했습니다. 브라우저에서 직접 결제를 진행해주세요.", "warning")
                return True

            naver_pay_page.wait_for_load_state("domcontentloaded", timeout=15000)
            self.log("[CGV] 네이버페이 결제창 로드 완료", "info")

            if developer_mode:
                self.log(
                    "[개발자 모드] CGV 결제수단(N pay) 선택 및 약관 동의 후 네이버페이 결제창을 정상적으로 열었습니다.",
                    "success",
                )
                self.log(
                    "[개발자 모드] 실제 결제 승인 방지를 위해 네이버페이의 최종 '동의하고 결제하기' 버튼 클릭은 건너뜁니다.",
                    "warning",
                )
                return True

            naver_pay_page.wait_for_timeout(1000)
            agree_candidates = [
                naver_pay_page.get_by_text("동의하고 결제하기", exact=True).first,
                naver_pay_page.locator("button:has-text('동의하고 결제하기')").first,
                naver_pay_page.locator("button:has-text('결제하기')").last,
            ]
            clicked_agree = False
            for btn in agree_candidates:
                try:
                    if btn.count() > 0 and btn.is_visible():
                        btn.click(timeout=5000)
                        clicked_agree = True
                        break
                except Exception:
                    continue

            if not clicked_agree:
                self.log("네이버페이 결제 버튼을 찾지 못했습니다. 팝업창에서 직접 결제를 완료해주세요.", "warning")
                return True

            # 8. Check if initial payment succeeded or failed (e.g. insufficient funds)
            naver_pay_page.wait_for_timeout(2500)
            if naver_pay_page.is_closed():
                self.log("[CGV] 🎉 네이버페이 기본 카드로 결제를 완료했습니다!", "success")
                return True

            # If popup is still open, check for decline / insufficient balance and fallback to Money / Bank account
            try:
                body_text = naver_pay_page.locator("body").inner_text(timeout=2000)
            except Exception:
                body_text = ""

            has_error = any(
                keyword in body_text
                for keyword in ("부족", "잔액", "거절", "실패", "다른 결제", "한도", "오류")
            )
            if not naver_pay_page.is_closed():
                self.log("[CGV] 1순위 카드 결제 미완료/잔액 부족 감지 · 2순위 네이버페이 머니/통장 결제로 자동 전환", "warning")
                money_candidates = [
                    naver_pay_page.locator("div, button, a").filter(has_text="머니 통장").last,
                    naver_pay_page.locator("div, button, a").filter(has_text="네이버페이 머니").last,
                    naver_pay_page.locator("div, button, a").filter(has_text="머니").last,
                ]
                for m_btn in money_candidates:
                    try:
                        if m_btn.count() > 0 and m_btn.is_visible():
                            m_btn.click(timeout=2000)
                            self.log("[CGV] 2순위 네이버페이 머니/통장 선택 완료", "info")
                            break
                    except Exception:
                        continue

                naver_pay_page.wait_for_timeout(500)
                for btn in agree_candidates:
                    try:
                        if btn.count() > 0 and btn.is_visible():
                            btn.click(timeout=5000)
                            self.log("[CGV] 🎉 네이버페이 머니/통장으로 최종 결제를 재시도했습니다!", "success")
                            return True
                    except Exception:
                        continue

            return True
        except Exception as exc:
            self.log(f"CGV 네이버페이 결제 진행 중 오류: {format_exception(exc)}", "warning")
            return True

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
                      const buttons = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
                      const target = buttons.find(b => clean(b.textContent) === '선택완료' && !b.disabled && b.getAttribute('aria-disabled') !== 'true');
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
        while not self.stop_event.is_set() and time.monotonic() - start_time < 10.0:
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

        if fallback_reason == "rate-limited":
            cooldown = self.RATE_LIMIT_BROWSER_RELOAD_INTERVAL
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
        rate_limited = fallback_reason == "rate-limited"
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
                try:
                    page.wait_for_timeout(400)
                except Exception:
                    self.stop_event.wait(0.4)
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

    def make_reservation_thread(self, reservation_data: dict[str, Any]) -> None:
        from playwright.sync_api import sync_playwright

        metadata = reservation_data.get("engine_metadata", {})
        cgv = metadata.get("cgv", {}) if isinstance(metadata, dict) else {}
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
                while not self.stop_event.is_set():
                    try:
                        result = self._race_schedule(page, schedule_url, concurrency)
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
                        self.stop_event.wait(poll_interval)
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
                if not self._enter_visitor_page(page, schedule):
                    keep_open = True
                    return
                if not self._select_visitors(page, people):
                    keep_open = True
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
                    self._proceed_naver_pay_checkout(page, developer_mode=developer_mode)
                    if developer_mode:
                        msg = "개발자 테스트 모드: 네이버페이 결제창까지 정상 진입했습니다 (최종 결제 미실행)."
                    else:
                        msg = "CGV 좌석 선점 및 네이버페이 결제 진행을 완료했습니다."
                    result = BookingResult(
                        True,
                        msg,
                        details={"site_no": site_no, "movie": movie},
                    )
                    if self.notify_success(result):
                        self.log(result.message, "success")
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
