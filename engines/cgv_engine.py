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
    FAST_MONITOR_READ_INTERVAL = 0.025
    FAST_MONITOR_MAX_CONSECUTIVE_ERRORS = 5
    HEDGE_DELAY_MS = 110
    MAX_BACKOFF = 15.0

    def __init__(self, log_callback, success_callback=None, **kwargs) -> None:
        super().__init__(log_callback, success_callback, **kwargs)
        self.scan_concurrency = 1
        self._browser_lock = threading.Lock()

    def start_reservation(
        self, reservation_data: dict[str, Any], num_threads: int, is_async: bool = False
    ) -> None:
        self.scan_concurrency = max(1, min(int(num_threads), CGV_MAX_WORKERS))
        self.log(
            f"CGV 고속 API 감시를 최대 {self.scan_concurrency}개 동시 연결로 시작합니다. "
            f"좌석 요청은 {self.FAST_SEAT_LAUNCH_INTERVAL_MS}ms 간격으로 교차 실행하며 "
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

    def _select_api_seats_in_ui(self, page, payload, selected) -> bool:
        self._sync_seat_payload_to_ui(page, payload)
        page.wait_for_timeout(50)
        for seat in selected:
            if not self._click_seat_by_id(page, seat.seat_id):
                return False
        return True

    @staticmethod
    def _click_seat_by_id(page, seat_id: str) -> bool:
        locator = page.locator(f'button[data-seatlocno="{seat_id}"]')
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click(timeout=1500)
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _install_cached_hold_responses(page, price_response, hold_response) -> None:
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
                    seatsByLabel.set(
                      normalize(`${seat.seatRowNm || ''}${seat.seatNo || ''}`),
                      seat,
                    );
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

    @staticmethod
    def _read_fast_seat_monitor(page) -> dict[str, Any]:
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
        auth = self._browser_auth_data(page)
        seat_url = self._seat_url(schedule, auth.get("custNo", ""))
        direct_hold = (
            None
            if developer_mode
            else self._direct_hold_config(schedule, people, auth, cgv)
        )
        preferred_concurrency = self.scan_concurrency
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
                    self.log("CGV 로그인 인증이 만료되어 브라우저 확인 경로로 전환합니다.", "warning")
                    return False, True
                if snapshot.get("terminalError"):
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

            if developer_mode:
                if not self._select_api_seats_in_ui(page, seat_payload, selected):
                    self.log("CGV 화면 동기화에 실패해 브라우저 안전 경로로 전환합니다.", "warning")
                    return False, True
                self.log(f"선택 좌석 확보 가능: {', '.join(group.seats)}", "success")
                self.log(
                    "개발자 테스트 모드로 좌석을 선택한 상태에서 정지합니다. 임시선점 요청은 보내지 않았습니다.",
                    "success",
                )
                return False, False

            transaction = hit.get("transaction")
            if not isinstance(transaction, dict):
                self.log("CGV 브라우저 내부 선점 결과가 없어 안전 경로로 전환합니다.", "warning")
                return False, True
            price_response = transaction.get("priceResponse")
            hold_response = transaction.get("holdResponse")
            hold_payload = transaction.get("holdPayload")
            if not all(
                isinstance(value, dict)
                for value in (price_response, hold_response, hold_payload)
            ):
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
            self.log(
                "CGV 결제 화면 연결이 확인되지 않아 임시선점을 취소하고 브라우저 안전 경로로 전환합니다.",
                "warning",
            )
            return False, True
        return False, False

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

    @staticmethod
    def _click_visible_by_text(page, labels: tuple[str, ...]) -> bool:
        for label in labels:
            locator = page.get_by_text(label, exact=True)
            for index in range(locator.count()):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        candidate.click(timeout=2500)
                        return True
                except Exception:
                    continue
        return False

    def _select_visitors(self, page, people: int) -> bool:
        """Select the normal visitor count and open CGV's seat modal.

        The current one-page booking UI exposes numbered buttons (1..8), not
        +/- controls.  The seat map is a modal on the same
        ``/cnm/selectVisitorCnt`` route, so URL changes are not a readiness
        signal.
        """
        page.wait_for_timeout(500)
        result = page.evaluate(
            r"""
            people => {
              const clean = value => (value || '').replace(/\s+/g, '');
              const nodes = [...document.querySelectorAll('body *')];
              const label = nodes.find(node => clean(node.textContent) === '일반');
              if (!label) return {ok:false, reason:'adult-label'};
              let box = label;
              for (let i=0; i<7 && box; i++, box=box.parentElement) {
                const target = [...box.querySelectorAll('button')].find(button =>
                  !button.disabled && clean(button.textContent) === String(people)
                );
                if (target) {
                  target.click();
                  return {ok:true};
                }
              }
              return {ok:false, reason:'number-button'};
            }
            """,
            max(1, people),
        )
        if not isinstance(result, dict) or not result.get("ok"):
            self.log("CGV 관람 인원 선택 버튼을 찾지 못했습니다.", "error")
            return False
        opened = page.evaluate(
            r"""
            () => {
              const clean = value => (value || '').replace(/\s+/g, '');
              const hint = [...document.querySelectorAll('p,div')].find(node =>
                clean(node.textContent) === '좌석을선택해주세요'
              );
              let box = hint;
              for (let i=0; i<6 && box; i++, box=box.parentElement) {
                const button = [...box.querySelectorAll('button')].find(node =>
                  !node.disabled && clean(node.textContent) === '선택'
                );
                if (button) { button.click(); return true; }
              }
              return false;
            }
            """
        )
        if not opened:
            self.log("CGV 좌석 모달의 선택 버튼을 찾지 못했습니다.", "error")
            return False
        try:
            page.locator("button[data-seatlocno]").first.wait_for(
                state="visible", timeout=15000
            )
        except Exception:
            self.log("CGV 좌석 정보가 제한 시간 안에 열리지 않았습니다.", "warning")
            return False
        return True

    @staticmethod
    def _available_seat_elements(page) -> list[dict[str, Any]]:
        result = page.evaluate(
            """
            () => [...document.querySelectorAll('button[data-seatlocno]')].map(node => {
              const label = node.textContent || '';
              const classes = String(node.className || '').toLowerCase();
              const unavailable = node.disabled || node.getAttribute('aria-disabled') === 'true' ||
                                  /disabled|complete|sold|reserved/.test(classes);
              return {id: node.getAttribute('data-seatlocno') || '', label, unavailable};
            })
            """
        )
        return [dict(item) for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    @staticmethod
    def choose_available_group(
        elements: list[dict[str, Any]], groups: tuple[CgvSeatGroup, ...]
    ) -> tuple[CgvSeatGroup, dict[str, str]] | None:
        available: dict[str, str] = {}
        for element in elements:
            if element.get("unavailable"):
                continue
            label = str(element.get("label", ""))
            for match in re.finditer(r"([A-Za-z가-힣]+)\s*[-_ ]?\s*([0-9]+)", label):
                available[normalize_seat_name("".join(match.groups()))] = str(element.get("id", ""))
        for group in groups:
            if is_contiguous_seat_group(group.seats) and all(
                seat in available for seat in group.seats
            ):
                return group, {seat: available[seat] for seat in group.seats}
        return None

    def _select_and_hold_seats(
        self,
        page,
        groups: tuple[CgvSeatGroup, ...],
        people: int,
        developer_mode: bool,
    ) -> bool:
        last_reload = time.monotonic()
        while not self.stop_event.is_set():
            if self._is_block_page(page):
                self.log("CGV 접근 제한이 감지되어 좌석 조회를 중지했습니다.", "error")
                return False
            elements = self._available_seat_elements(page)
            chosen = self.choose_available_group(elements, groups)
            if chosen:
                group, ids = chosen
                for seat in group.seats:
                    if not self._click_seat_by_id(page, ids[seat]):
                        self.log(f"CGV 좌석 {seat} 선택 버튼을 누르지 못했습니다.", "warning")
                        return False
                self.log(f"선택 좌석 확보 가능: {', '.join(group.seats)}", "success")
                if developer_mode:
                    self.log(
                        "개발자 테스트 모드로 좌석을 선택한 상태에서 정지합니다. 임시선점 요청은 보내지 않았습니다.",
                        "success",
                    )
                    return False
                if not self._click_visible_by_text(page, ("선택완료", "선택 완료")):
                    self.log("좌석 임시선점 진행 버튼을 찾지 못했습니다.", "error")
                    return False
                try:
                    page.get_by_text("결제하기", exact=True).last.wait_for(
                        state="visible", timeout=15000
                    )
                    return True
                except Exception:
                    # A seat can disappear between rendering and the atomic
                    # seatTempPrmp call. Acknowledge CGV's normal conflict
                    # dialog and continue the watch instead of reporting a
                    # false success.
                    self._click_visible_by_text(page, ("확인",))
                    self.silent_tick("CGV 좌석 선점 경합 발생 · 다시 조회")
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                    if not self._select_visitors(page, people):
                        return False
                    last_reload = time.monotonic()
                    continue

            self.silent_tick("선택한 CGV 좌석 묶음이 아직 비어 있지 않습니다")
            now = time.monotonic()
            if now - last_reload >= 1.5:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                if not self._select_visitors(page, people):
                    return False
                last_reload = now
            else:
                page.wait_for_timeout(300)
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
                browser = playwright.chromium.connect_over_cdp(chrome.endpoint)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
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
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                    if self._select_visitors(page, people):
                        held = self._select_and_hold_seats(
                            page, groups, people, developer_mode
                        )
                keep_open = True
                if held:
                    result = BookingResult(
                        True,
                        "CGV 좌석 임시선점을 완료했습니다. 열린 Chrome에서 제한 시간 안에 결제해주세요.",
                        details={"site_no": site_no, "movie": movie},
                    )
                    if self.notify_success(result):
                        self.log(result.message, "success")
        except Exception as exc:
            if not self.stop_event.is_set():
                self.log(f"CGV 예약 흐름 오류: {format_exception(exc)}", "error")
                keep_open = True
        finally:
            if keep_open:
                self._release_browser_lease_when_closed(chrome)
            else:
                chrome.close_if_launched()
                chrome.release()
