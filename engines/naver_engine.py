"""Naver Booking engine: poll the API, submit through the page.

Shape of this engine
--------------------
It keeps trying until the slot is bookable. There is no clock trigger and no
"wait for the top of the hour" -- the same shape as the other HTTP engines in this
project: a loop that asks the server "can I book this yet?" and acts the moment
the answer changes.

The question is asked over GraphQL (see ``engines/naver_api.py``), not by reading
the rendered page. ``hourlySchedule`` answers without a login, returns each slot's
own ``stock``, and -- crucially -- omits dates that have not opened yet. A date
appearing in the response is therefore the open signal, and it shows up
immediately rather than whenever a page reload happens to land at the right
moment.

Submission still goes through the page. Naver's own bundle ships no booking
mutation (all 21 it contains are account/coupon/visitor operations), so the
booking request is not something that can be reconstructed with any confidence.
Letting the browser walk the real flow means Naver validates it exactly as it
would for a person.

What this replaces
------------------
The previous version had three defects that together meant it could not book at
all:

* ``page.evaluate_handle(...).as_element()`` was called without ``await``. In the
  async API that is a coroutine, so ``.as_element()`` raised ``AttributeError``
  every single time, the enclosing ``except`` swallowed it, and the time slot was
  never clicked.
* ``target_open_timestamp`` was initialised to ``None`` and never assigned, so the
  "refresh while waiting for the date to open" branch was unreachable. The engine
  span on a page it never reloaded.
* Agreement handling ticked *every* checkbox on the page, including optional
  marketing consents, and targeted build-hashed class names
  (``AgreementDesc__section_inner__Ny+MK``) that no longer exist -- and could not
  have matched anyway, since an unescaped ``+`` is not a valid CSS selector.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from typing import Any

from engines.base_engine import BaseEngine
from engines.naver_api import (
    NaverApiError,
    NaverBookingApi,
    NaverServerClock,
    NaverSlot,
    parse_ids,
    participant_option,
)
from pengucro.storage import SecretStore, data_path, load_json


# Naver renders the timetable as, verified against the live page:
#
#   ul.time_list > li.time_item > button.btn_time                "오후 1:20 1매"
#   ul.time_list > li.time_item > button.btn_time.unselectable   "오후 2:30 매진"
#
# A sold-out slot keeps identical markup and is marked *only* by the
# ``unselectable`` class and the word 매진. ``disabled`` stays false and there is
# no ``aria-disabled``, so neither attribute can tell the two apart.
#
# The element is tagged with an attribute and then addressed with an ordinary
# locator. Handing an index into querySelectorAll back to Python is stale the
# instant React re-renders, and needed the ElementHandle round trip that was the
# original source of the missing-await bug.
SLOT_TAG_SCRIPT = r"""(targetMinutes) => {
    const TAG = 'data-pengucro-slot';
    document.querySelectorAll('[' + TAG + ']').forEach(el => el.removeAttribute(TAG));

    const minutesOf = (text) => {
        const stamps = text.match(/\d{1,2}\s*:\s*\d{2}/g) || [];
        // Two or more times in one element means it is a container, not a slot.
        if (stamps.length !== 1) return null;
        const parts = stamps[0].match(/(\d{1,2})\s*:\s*(\d{2})/);
        let hour = parseInt(parts[1], 10);
        const minute = parseInt(parts[2], 10);
        if (text.includes('오후') && hour < 12) hour += 12;
        else if (text.includes('오전') && hour === 12) hour = 0;
        return hour * 60 + minute;
    };

    const known = Array.from(document.querySelectorAll(
        'ul.time_list li.time_item button, button.btn_time'));
    // The broad sweep is a fallback only, so a layout change degrades rather
    // than breaks outright.
    const pool = known.length ? known
        : Array.from(document.querySelectorAll('button, a, li'));

    const result = {
        rendered: document.querySelectorAll('li.time_item').length,
        pool: pool.length,
        match: null,
    };
    for (const el of pool) {
        const text = (el.innerText || '').trim();
        if (!text || minutesOf(text) !== targetMinutes) continue;

        const rect = el.getBoundingClientRect();
        const klass = (el.className || '').toString().toLowerCase();
        const state = {
            text: text.replace(/\s+/g, ' ').slice(0, 60),
            visible: rect.width > 0 || rect.height > 0,
            soldOut: /매진|마감|불가|종료/.test(text),
            blockedClass: klass.includes('unselectable') || klass.includes('disable'),
            disabledAttr: el.hasAttribute('disabled')
                || el.getAttribute('aria-disabled') === 'true',
        };
        state.clickable = state.visible && !state.soldOut
            && !state.blockedClass && !state.disabledAttr;
        if (state.clickable) {
            el.setAttribute(TAG, '1');
            result.match = state;
            return result;
        }
        if (result.match === null) result.match = state;
    }
    return result;
}"""

# Confirms the click landed: the chosen button gains a `selected` class and the
# quantity stepper plus the 다음 button appear. Clicking a time does not navigate.
SLOT_SELECTED_SCRIPT = r"""() => {
    const chosen = document.querySelector('li.time_item button[class*="selected"]');
    return chosen ? (chosen.innerText || '').replace(/\s+/g, ' ').slice(0, 60) : null;
}"""

# Finding 다음 is not as simple as it looks. Two elements on the item page match
# ``[class*="btn_next"]``:
#
#   button.btn_next                       18x24, no text  <- calendar month arrow
#   button.NextButton__btn_next__mKL_0   688x44, "다음"   <- the one we want
#
# In DOM order the calendar arrow comes first, so a ``.first`` locator flipped the
# calendar to the next month and the run then sat waiting ten seconds for a page
# transition that was never going to happen.
NEXT_BUTTON_SCRIPT = r"""() => {
    const TAG = 'data-pengucro-next';
    document.querySelectorAll('[' + TAG + ']').forEach(el => el.removeAttribute(TAG));

    const scored = [];
    for (const el of document.querySelectorAll('button, a')) {
        const cls = (el.className || '').toString();
        const text = (el.innerText || '').trim();
        const rect = el.getBoundingClientRect();
        let score = 0;
        if (/NextButton__btn_next/.test(cls)) score = 3;
        else if (text === '다음' && rect.width >= 80) score = 2;
        else if (text === '다음') score = 1;
        if (!score) continue;
        scored.push({ el: el, score: score, cls: cls.slice(0, 60), text: text,
                      w: Math.round(rect.width), h: Math.round(rect.height) });
    }
    if (!scored.length) return null;
    scored.sort((a, b) => b.score - a.score);
    const best = scored[0];
    best.el.setAttribute(TAG, '1');
    return { cls: best.cls, text: best.text, w: best.w, h: best.h,
             score: best.score, candidates: scored.length };
}"""

# The request page has no <select>, no <input> and no checkbox at all -- verified
# in a live logged-in session. The extra question is a custom control:
#
#   button.select_btn        "해당하는 항목을 선택해주세요."   <- opens the list
#   button.select_item       "2인" / "3인" / "4인" / "5인"     <- the options
#
# Reader details come from the account rather than form fields (there is a 변경
# button to change them), and consent is carried by the submit button's own label,
# which is why no checkbox exists to tick.
OPEN_SELECT_SCRIPT = r"""() => {
    const trigger = document.querySelector('button.select_btn, button[class*="select_btn"]');
    if (!trigger) return null;
    trigger.click();
    return (trigger.innerText || '').replace(/\s+/g, ' ').slice(0, 40);
}"""

PICK_OPTION_SCRIPT = r"""(wanted) => {
    const items = Array.from(document.querySelectorAll(
        'button.select_item, button[class*="select_item"], [role="option"]'));
    for (const item of items) {
        if ((item.innerText || '').trim() === wanted) {
            item.click();
            return true;
        }
    }
    return items.map(i => (i.innerText || '').trim()).slice(0, 12);
}"""

SELECTED_OPTION_SCRIPT = r"""() => {
    const trigger = document.querySelector('button.select_btn, button[class*="select_btn"]');
    return trigger ? (trigger.innerText || '').replace(/\s+/g, ' ').slice(0, 40) : null;
}"""

# Consent is granted deliberately, not by sweeping the page. An "agree to all"
# toggle is used when the page offers one because it cascades the way the site
# intends; otherwise only boxes the page itself marks 필수 are ticked. Optional
# consents are left exactly as the user left them.
CONSENT_SCRIPT = r"""() => {
    const boxes = () => Array.from(document.querySelectorAll('input[type="checkbox"]'));
    const scopeText = (el) => {
        const scope = el.closest('label, li, dd, div, section') || el;
        return (scope.innerText || '').replace(/\s+/g, ' ');
    };
    const clicked = [];

    for (const box of boxes()) {
        if (/전체\s*동의|모두\s*동의/.test(scopeText(box))) {
            if (!box.checked) {
                box.click();
                if (box.checked) clicked.push('전체동의');
            }
            break;
        }
    }

    for (const box of boxes()) {
        if (box.checked) continue;
        if (box.required || /필수/.test(scopeText(box))) {
            box.click();
            if (box.checked) clicked.push(box.name || box.id || '필수항목');
        }
    }

    const all = boxes();
    return {
        clicked: clicked,
        total: all.length,
        checked: all.filter(b => b.checked).length,
        unchecked: all.filter(b => !b.checked)
                      .map(b => scopeText(b).slice(0, 40)),
    };
}"""

# Native <select> first: it is what the sample business renders, and setting it
# directly is both faster and immune to dropdown markup changes.
SELECT_OPTION_SCRIPT = r"""(wanted) => {
    for (const select of document.querySelectorAll('select')) {
        for (const option of Array.from(select.options)) {
            if ((option.textContent || '').trim() === wanted) {
                select.value = option.value;
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
        }
    }
    return false;
}"""

# Account state is published in the page's own Apollo cache, so login can be
# confirmed from a page we were going to load anyway.
LOGIN_STATE_SCRIPT = r"""() => {
    const state = window.__APOLLO_STATE__ || {};
    for (const key of Object.keys(state)) {
        const entry = state[key];
        if (entry && entry.__typename === 'Account') return !!entry.isLoggedIn;
    }
    return null;
}"""


class NaverEngine(BaseEngine):
    """Polls Naver's schedule API and books through the page when a slot frees up."""

    # Polling cadence, set from measurement rather than guesswork.
    #
    # Measured against the live endpoint (2026-07-26, single connection):
    #   * short bursts of 12 calls: clean at 1, 2, 4, 8 and 16 req/s targets. The
    #     16 target only achieved 9.9/s because calls are sequential and one round
    #     trip is ~86 ms, so ~11 req/s is the hard ceiling of this shape.
    #   * 480 calls at ~12 req/s over 39 s: zero failures.
    #   * 901 calls at 3.00 req/s over 5 minutes: zero failures, and latency did
    #     not drift (first-100 p50 88 ms vs last-100 p50 86 ms), so nothing was
    #     being throttled behind the scenes.
    #   * one poll costs ~440 B out and ~1.9 kB back. No rate-limit headers are
    #     exposed; the response carries `cache-control: no-store`.
    #
    # So 0.3 s is comfortably inside what the endpoint tolerates while being more
    # than three times faster than the 1.0 s this engine started with. BURST is
    # used where a change can land at any instant. What is *not* measured is
    # behaviour over many hours, which is the only reason RELAXED exists: after a
    # long stretch with nothing changing at all, an overnight watch drops to about
    # a third of the request volume. Any observed change snaps it straight back.
    POLL_BASE_SECONDS = 0.2
    POLL_BURST_SECONDS = 0.1
    POLL_RELAXED_SECONDS = 1.0
    RELAX_AFTER_SECONDS = 1800.0
    BURST_WINDOW_SECONDS = 120.0
    CLOCK_RESYNC_SECONDS = 300.0

    # The parked tab is reloaded in two situations.
    #
    # The one that matters: a date that has not opened renders no timetable at all,
    # so the warm-click path has nothing to click until the page is reloaded after
    # the date appears in the API. That reload happens immediately on the
    # transition.
    #
    # The other is plain staleness, and the interval is deliberately long. A reload
    # takes about two seconds during which the poll loop is blind, so refreshing
    # every few minutes trades away more detection time than the freshness is
    # worth -- especially since _submit reloads by itself whenever the button it
    # expects is not there.
    REWARM_INTERVAL_SECONDS = 1800.0
    REWARM_MIN_GAP_SECONDS = 15.0

    SUBMIT_MAX_ATTEMPTS = 10
    PAGE_TIMEOUT_MS = 15000
    NAVIGATION_TIMEOUT_MS = 20000
    LOGIN_WAIT_SECONDS = 300.0
    CDP_CONNECT_TIMEOUT_MS = 12000
    # After the page contradicts the API and says 매진, wait this long before
    # driving the page again. The API poll keeps running at full speed regardless.
    TAKEN_BACKOFF_SECONDS = 0.6
    # Booking tabs left behind by earlier runs or by the user poking around. They
    # are closed at startup so attaching stays fast; anything unrelated is left be.
    STALE_TAB_LIMIT = 3

    # The item page is a React app: at domcontentloaded its body holds 68
    # characters and the timetable does not exist. Measured render milestones on
    # the live page were ~500 ms for the shell and ~2 s for ul.time_list. Waiting
    # on domcontentloaded therefore scanned an empty document, which is exactly
    # why every submit reported "버튼을 페이지에서 찾지 못했습니다" within a second.
    TIMETABLE_SELECTOR = "ul.time_list li.time_item"
    TIMETABLE_TIMEOUT_MS = 12000
    # Shorter budget once a run is underway. Rendering was measured at ~1.7 s on a
    # warm browser, so six seconds is ample -- and the difference matters: every
    # second spent waiting here is a second the API poll is not running. The full
    # 12 s is kept only for the first load, which pays browser start-up costs.
    TIMETABLE_RETRY_TIMEOUT_MS = 6000

    def __init__(
        self,
        log_callback,
        success_callback=None,
        status_callback=None,
        log_batch_callback=None,
        event_callback=None,
    ) -> None:
        super().__init__(
            log_callback, success_callback, status_callback,
            log_batch_callback, event_callback,
        )
        self.secret_store = SecretStore()

        config = load_json("config.json", {})
        if not isinstance(config, dict):
            config = {}
        # config.json switches:
        #   "naver_use_real_chrome": false -> always use a throwaway Playwright
        #       profile and the stored-cookie login flow
        #   "naver_poll_interval": normal seconds between schedule polls
        #   "naver_poll_burst_interval": seconds between polls when a change can
        #       land at any instant (slot full, open moment imminent, retry)
        #   "naver_poll_relax_after": seconds of no change before easing off;
        #       set to 0 to never ease off and stay at full speed indefinitely
        self._use_real_chrome = bool(config.get("naver_use_real_chrome", True))
        self._close_chrome_on_exit = bool(config.get("naver_close_chrome_on_exit", False))
        self._poll_base = max(0.1, float(config.get("naver_poll_interval",
                                                    self.POLL_BASE_SECONDS)))
        self._poll_burst = max(0.05, float(config.get("naver_poll_burst_interval",
                                                      self.POLL_BURST_SECONDS)))
        self._relax_after = max(0.0, float(config.get("naver_poll_relax_after",
                                                      self.RELAX_AFTER_SECONDS)))

        self.api: NaverBookingApi | None = None
        self.clock: NaverServerClock | None = None
        self.browser = None
        self._context = None
        self._page = None
        self._owns_browser = False
        self._chrome_session = None
        self._playwright = None
        self._participant: tuple[str, str] | None = None
        self._item_url = ""
        self._log_marks: dict[str, float] = {}
        self._dialog_state: dict[str, str] = {"message": ""}
        self._open_at_epoch: float | None = None
        self._last_warm = 0.0
        self._warmed_for_date = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start_reservation(self, reservation_data, num_threads, is_async=False):
        """One worker, always, and always on the async path.

        Extra workers cannot win anything here: a single Naver account can hold a
        single booking, ``submission_lock`` already serialises attempts, and every
        additional worker is another browser context loading the same page from
        the same session. The old default of five bought nothing and multiplied
        the footprint by five.
        """
        target = int(num_threads or 1)
        if target > 1:
            self.log(
                f"네이버는 계정 하나로 한 건만 예약할 수 있어 동시 시도 수를 "
                f"{target} → 1로 조정합니다.",
                "info",
            )
        self.log("네이버는 탭 1개로 동작합니다. (여러 탭을 열지 않습니다)", "info")
        super().start_reservation(reservation_data, 1, is_async=True)

    async def run_async_tasks(self, reservation_data, num_tasks, start_idx_offset=0):
        try:
            await super().run_async_tasks(reservation_data, num_tasks, start_idx_offset)
        except NaverApiError as exc:
            # Setup failures carry a message written for the user; letting them
            # bubble up would surface as a bare "비동기 예약 실행 오류".
            if not self.stop_event.is_set():
                self.log(f"[에러] {exc}", "error")
        finally:
            await self._teardown_browser()

    def stop_reservation(self) -> None:
        # Only signals. Closing the browser from this thread is what used to race
        # the worker mid-navigation; the worker tears its own browser down.
        super().stop_reservation()

    # ------------------------------------------------------------------
    # Setup (BaseEngine calls this before the workers start)
    # ------------------------------------------------------------------
    async def pre_fetch_sessions_async(self, num_sessions, reservation_data):
        self.session_pool = []
        booking_url = self._resolve_url(reservation_data)
        if not booking_url:
            raise NaverApiError(
                "유효한 네이버 예약 URL이 아닙니다. 테마 주소에 /items/ 가 포함되어야 합니다."
            )

        ids = parse_ids(booking_url)
        if not ids:
            raise NaverApiError(
                "네이버 예약 URL에서 상품 번호를 찾지 못했습니다. "
                "(예: .../bizes/1498729/items/7094790)"
            )
        service_id, business_id, item_id = ids

        self.api = NaverBookingApi(business_id, item_id, service_id, log=self.log)
        self._item_url = self.api.item_url
        self.clock = NaverServerClock(self.api, log=self.log)

        target_date = str(reservation_data.get("reservationDate") or "")
        target_time = str(reservation_data.get("reservationTime") or "")[:5]
        if not target_date or not target_time:
            raise NaverApiError("예약 날짜와 시간을 먼저 선택해주세요.")

        meta = await asyncio.to_thread(self.api.fetch_item_meta)
        self.log(
            f"네이버 예약 준비 · {meta.name or '상품'} · {target_date} {target_time}",
            "info",
        )
        await asyncio.to_thread(self.clock.sync, True)

        blocked = meta.hard_block()
        if blocked:
            raise NaverApiError(blocked)

        # Open-time information is reported, never waited on: this engine polls.
        # It is only used to decide when to poll harder.
        self._open_at_epoch = (
            meta.open_at.timestamp()
            if (meta.uses_open_schedule and meta.open_at) else None
        )
        if meta.uses_open_schedule and meta.open_at:
            remaining = self.clock.seconds_until(meta.open_at.timestamp())
            if remaining > 0:
                self.log(
                    f"[정보] 예약 오픈 예정 {meta.open_at:%Y-%m-%d %H:%M} · "
                    f"서버 시간 기준 {self._format_remaining(remaining)} 남음 · "
                    "오픈을 기다리지 않고 계속 확인합니다.",
                    "info",
                )
            else:
                self.log(
                    f"[정보] 예약 오픈 {meta.open_at:%Y-%m-%d %H:%M} (이미 지남)", "info"
                )

        form = meta.custom_form or await asyncio.to_thread(self.api.fetch_business_form)
        people = str(reservation_data.get("people") or "").strip()
        self._participant = participant_option(form, people) if people else None
        if form and people and not self._participant:
            available = [
                option.get("value")
                for question in form
                if isinstance(question, dict)
                for option in (question.get("options") or [])
                if isinstance(option, dict)
            ]
            self.log(
                f"[경고] 인원 '{people}'에 해당하는 선택지를 찾지 못했습니다. "
                f"선택 가능: {', '.join(str(v) for v in available if v) or '없음'}",
                "warning",
            )
        elif self._participant:
            self.log(f"[정보] 추가 입력 '{self._participant[0]}' → "
                     f"'{self._participant[1]}' 로 채웁니다.", "info")

        self.log(
            f"[정보] 스캔 주기 기본 {self._poll_base:.2f}초({1 / self._poll_base:.1f}회/초) · "
            f"집중 {self._poll_burst:.2f}초({1 / self._poll_burst:.1f}회/초)"
            + (f" · {int(self._relax_after // 60)}분간 변화 없으면 "
               f"{self.POLL_RELAXED_SECONDS:.1f}초로 절전"
               if self._relax_after else " · 절전 없음"),
            "info",
        )

        # A first read tells the user where things stand before the loop starts.
        try:
            slot = await asyncio.to_thread(self.api.find_slot, target_date, target_time)
        except NaverApiError as exc:
            self.log(f"[경고] 첫 조회 실패: {exc}", "warning")
            slot = None
        if slot is None:
            reason = "아직 예약 창이 열리지 않았습니다"
            self.log(
                f"[정보] {target_date} {target_time} 슬롯이 아직 열리지 않았습니다. "
                "열릴 때까지 계속 확인합니다.",
                "info",
            )
        else:
            reason = slot.blocked_reason(self.clock.now_kst())
            self.log(
                f"[정보] 슬롯 확인 · slotId={slot.slot_id} "
                f"잔여 {slot.remaining}/{slot.stock} · "
                f"{'예약 가능' if reason is None else reason}",
                "info",
            )
        # Seed the change detector so the loop's first turn does not repeat what
        # was just reported.
        self._last_signature = self._slot_signature(slot, reason)

        await self._open_browser(reservation_data)

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------
    async def make_reservation_async_task(self, reservation_data, task_idx):
        target_date = str(reservation_data.get("reservationDate") or "")
        target_time = str(reservation_data.get("reservationTime") or "")[:5]
        dev_mode = bool(reservation_data.get("devMode", False))
        assert self.api is not None and self.clock is not None

        last_resync = time.monotonic()
        submit_attempts = 0
        last_signature: tuple[Any, ...] | None = getattr(self, "_last_signature", None)
        last_change = time.monotonic()

        while not self.stop_event.is_set():
            if time.monotonic() - last_resync >= self.CLOCK_RESYNC_SECONDS:
                last_resync = time.monotonic()
                await asyncio.to_thread(self.clock.sync, False)

            try:
                slot = await asyncio.to_thread(
                    self.api.find_slot, target_date, target_time
                )
            except NaverApiError as exc:
                self.silent_tick(f"{target_time} 조회 실패")
                self._log_throttled("poll_error", f"[경고] 조회 실패: {exc}", "warning", 15.0)
                # Back off on transport trouble instead of hammering a sore spot.
                await asyncio.sleep(max(self._poll_base, 1.0))
                continue

            reason = (
                "아직 예약 창이 열리지 않았습니다"
                if slot is None
                else slot.blocked_reason(self.clock.now_kst())
            )

            signature = self._slot_signature(slot, reason)
            if signature != last_signature:
                last_signature = signature
                last_change = time.monotonic()
                if slot is None:
                    self.log(f"[정보] {target_time} · 슬롯 미개설", "info")
                else:
                    self.log(
                        f"[정보] {target_time} 상태 변화 · slotId={slot.slot_id} "
                        f"잔여 {slot.remaining}/{slot.stock} · "
                        f"{'예약 가능' if reason is None else reason}",
                        "info",
                    )

            if slot is None or reason is not None:
                # Keep the parked tab useful. A date that has not opened renders no
                # timetable at all, so the warm-click path has nothing to click
                # until the page is reloaded *after* the date appears. Reload once
                # on that transition, and periodically so a tab left for hours is
                # not stale when it finally matters.
                await self._rewarm_if_needed(target_date, slot is not None)
                delay, tier = self._poll_delay(slot, last_change)
                self.silent_tick(f"{target_time} {reason}")
                self._log_throttled(
                    "waiting",
                    f"[정보] {target_date} {target_time} · {reason} · "
                    f"계속 확인 중 ({tier} {1 / delay:.1f}회/초)",
                    "info",
                    30.0,
                )
                await asyncio.sleep(delay)
                continue

            # ---- bookable ------------------------------------------------
            if submit_attempts >= self.SUBMIT_MAX_ATTEMPTS:
                self.log(
                    f"[에러] 제출을 {submit_attempts}회 시도했지만 완료되지 못했습니다. "
                    "브라우저에서 직접 확인해주세요.",
                    "error",
                )
                return

            if not self.submission_lock.acquire(blocking=False):
                await asyncio.sleep(0.05)
                continue
            try:
                self.log(
                    f"{target_time} 예약 가능 확인 (잔여 {slot.remaining}) · 제출을 시작합니다.",
                    "warning",
                )
                outcome, detail = await self._submit(slot, reservation_data, dev_mode)
                # Only our own breakage counts against the budget.
                #
                # "notready" means the page had not rendered, and "taken" means the
                # page says 매진 -- neither is a failed booking attempt, and neither
                # should end a watch that is meant to run for hours. Counting them
                # made the engine give up after fifteen seconds whenever the
                # schedule API ran briefly ahead of the rendered page.
                if outcome == "retry":
                    submit_attempts += 1
            finally:
                try:
                    self.submission_lock.release()
                except RuntimeError:
                    pass

            if outcome == "success":
                self.log(f"🎉 네이버 예약 성공! {detail}", "success")
                self.notify_success()
                return
            if outcome == "dev":
                while not self.stop_event.is_set():
                    await asyncio.sleep(0.5)
                return
            if outcome == "notready":
                self._record_attempt(f"{target_time} 화면 준비 대기")
                self._log_throttled(
                    "notready", f"[경고] {detail} · 화면을 다시 불러옵니다.",
                    "warning", 5.0,
                )
                await asyncio.sleep(0.5)
                continue
            if outcome == "taken":
                # Keep the signature: an identical failure is not a state change,
                # and clearing it made the loop re-announce the same slot on
                # every attempt.
                self._record_attempt(f"{target_time} 선점됨")
                self._log_throttled(
                    "taken",
                    f"[정보] 화면에는 아직 예약할 수 없습니다. 계속 확인합니다. {detail}",
                    "info", 10.0,
                )
                # The page just told us the slot is gone, so trust it over the API
                # reading that sent us here and stop driving the page for a moment.
                # Retrying at burst speed costs a ~1.4 s page cycle per turn and
                # tells us nothing the next API poll will not.
                await asyncio.sleep(max(self._poll_burst, self.TAKEN_BACKOFF_SECONDS))
                continue
            if outcome == "login":
                # A session can expire during a long watch. Giving up here would
                # throw away the whole wait, so ask for a re-login and carry on.
                self.log(f"[경고] 로그인이 필요합니다. {detail}", "warning")
                try:
                    await self._ensure_login(dev_mode)
                except NaverApiError as exc:
                    self.log(f"[에러] {exc}", "error")
                    return
                if self.stop_event.is_set():
                    return
                continue

            self._record_attempt(f"{target_time} 제출 실패")
            self.log(f"[경고] 제출 실패 · {detail} · 다시 시도합니다.", "warning")
            await asyncio.sleep(self._poll_burst)

    # ------------------------------------------------------------------
    # Browser
    # ------------------------------------------------------------------
    async def _open_browser(self, reservation_data) -> None:
        from playwright.async_api import async_playwright

        dev_mode = bool(reservation_data.get("devMode", False))
        self._playwright = await async_playwright().start()

        if self._use_real_chrome:
            from engines import browser_session

            session = await asyncio.to_thread(browser_session.start_or_attach, 9333, self.log)
            if session is not None:
                try:
                    # Bounded on purpose. Attaching walks every open target, so a
                    # cluttered or half-wedged profile can otherwise sit here for
                    # the full default 30 s before anything is reported.
                    self.browser = await self._playwright.chromium.connect_over_cdp(
                        session.endpoint, timeout=self.CDP_CONNECT_TIMEOUT_MS
                    )
                    self._chrome_session = session
                    self._owns_browser = False
                    self._context = (
                        self.browser.contexts[0]
                        if self.browser.contexts
                        else await self.browser.new_context()
                    )
                    await self._close_stale_tabs()
                    self._page = await self._context.new_page()
                    self.log(
                        "실제 Chrome 프로필에 연결했습니다. "
                        f"(프로필: {browser_session.profile_dir()})",
                        "info",
                    )
                except Exception as exc:
                    self.log(
                        f"[경고] Chrome 연결 실패, 내장 브라우저로 전환합니다. "
                        f"({type(exc).__name__}) 열려 있는 Chrome 창을 닫고 다시 "
                        f"시도하면 해결되는 경우가 많습니다.",
                        "warning",
                    )
                    try:
                        if self.browser is not None:
                            await self.browser.close()
                    except Exception:
                        pass
                    self.browser = None

        if self.browser is None:
            self.browser = await self._launch_bundled(headless=False)
            self._owns_browser = True
            self._context = await self.browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="ko-KR",
            )
            cookies = self._load_cookies()
            if cookies:
                try:
                    await self._context.add_cookies(cookies)
                except Exception:
                    self.log("[경고] 저장된 쿠키를 적용하지 못했습니다.", "warning")
            self._page = await self._context.new_page()

        self._page.set_default_timeout(self.PAGE_TIMEOUT_MS)
        self._page.set_default_navigation_timeout(self.NAVIGATION_TIMEOUT_MS)

        self._dialog_state = {"message": ""}

        async def on_dialog(dialog):
            self._dialog_state["message"] = dialog.message or ""
            try:
                await dialog.accept()
            except Exception:
                pass

        self._page.on("dialog", on_dialog)

        target_date = reservation_data.get("reservationDate")
        rendered = await self._goto_item(target_date)
        await self._ensure_login(dev_mode)
        if not rendered:
            # Not fatal: the date may simply have no timetable yet. Say so, since
            # it is the difference between "waiting for the window" and "broken".
            rendered = await self._goto_item(target_date)
        self._last_warm = time.monotonic()
        self._warmed_for_date = rendered
        self.log(
            f"[정보] {target_date} 시간표 "
            + ("준비 완료 · 감지되면 새로고침 없이 바로 클릭합니다."
               if rendered else "아직 없음 · 열리면 미리 불러옵니다."),
            "info",
        )

    async def _close_stale_tabs(self) -> None:
        """Tidy booking tabs left over from earlier runs.

        The tab leak is fixed at teardown, but a profile that already accumulated
        them -- or a run that was killed rather than stopped -- still needs
        clearing, because attaching walks every target. Only booking pages are
        touched; anything else the user has open is left alone.
        """
        if self._context is None:
            return
        stale = []
        for page in list(self._context.pages):
            try:
                url = page.url or ""
            except Exception:
                continue
            if "booking.naver.com" in url:
                stale.append(page)
        if len(stale) < self.STALE_TAB_LIMIT:
            return
        closed = 0
        for page in stale:
            try:
                await page.close()
                closed += 1
            except Exception:
                continue
        if closed:
            self.log(f"[정보] 이전 실행에서 남은 예약 탭 {closed}개를 정리했습니다.", "info")

    async def _launch_bundled(self, headless: bool):
        errors = []
        for channel in ("chrome", "msedge", None):
            try:
                if channel:
                    return await self._playwright.chromium.launch(
                        channel=channel, headless=headless
                    )
                return await self._playwright.chromium.launch(headless=headless)
            except Exception as exc:
                errors.append(f"{channel or 'default'}: {exc}")
        raise NaverApiError(f"브라우저 실행 실패 ({'; '.join(errors)})")

    def _item_url_for(self, target_date) -> str:
        if not target_date:
            return self._item_url
        # The parameter really does pre-select the day: the calendar comes back
        # with `.calendar_date.selected` on it, so no calendar click is needed.
        return f"{self._item_url}?startDateTime={target_date}T00:00:00%2B09:00"

    async def _goto_item(self, target_date, timeout_ms: int | None = None) -> bool:
        """Load the item page for a date and wait until the timetable exists."""
        await self._page.goto(self._item_url_for(target_date),
                              wait_until="domcontentloaded")
        return await self._wait_for_timetable(timeout_ms)

    async def _timetable_present(self) -> bool:
        """Is a timetable on screen right now? Never waits.

        Distinct from _wait_for_timetable on purpose. Asking that one for a
        zero-length wait does not work twice over: ``timeout_ms or DEFAULT`` turns
        0 into the 12 s default, and Playwright treats ``timeout=0`` as "no timeout"
        anyway. Either way the caller blocks -- which is what made the polling loop
        crawl to roughly one turn every twelve seconds on a date that renders no
        timetable at all.
        """
        if self._page is None:
            return False
        try:
            return await self._page.locator(self.TIMETABLE_SELECTOR).count() > 0
        except Exception:
            return False

    async def _wait_for_timetable(self, timeout_ms: int | None = None) -> bool:
        timeout = self.TIMETABLE_TIMEOUT_MS if timeout_ms is None else timeout_ms
        if timeout <= 0:
            return await self._timetable_present()
        try:
            await self._page.wait_for_selector(
                self.TIMETABLE_SELECTOR, timeout=timeout, state="attached",
            )
            return True
        except Exception:
            return False

    async def _rewarm_if_needed(self, target_date: str, date_exists: bool) -> None:
        """Reload the parked tab when it would otherwise be useless or stale."""
        if self._page is None:
            return
        now = time.monotonic()
        warm = await self._timetable_present()

        # The date just started existing in the API but the tab still shows no
        # timetable: reload so the click path is warm before it is needed.
        #
        # The minimum gap matters. If the reload does not produce a timetable --
        # which happens when the API lists a date the page will not render -- then
        # `_warmed_for_date` stays false and this condition holds again on the very
        # next turn, reloading every couple of seconds forever.
        transition = (
            date_exists and not warm and not self._warmed_for_date
            and now - self._last_warm >= self.REWARM_MIN_GAP_SECONDS
        )
        stale = now - self._last_warm >= self.REWARM_INTERVAL_SECONDS
        if not (transition or stale):
            return

        self._last_warm = now
        rendered = await self._goto_item(target_date)
        self._warmed_for_date = rendered
        if transition:
            self.log(
                f"[정보] {target_date} 시간표가 생겼습니다 · 화면을 미리 불러뒀습니다"
                + ("" if rendered else " (아직 렌더링되지 않음)"),
                "info",
            )
        elif rendered:
            self._log_throttled(
                "rewarm", "[정보] 대기 화면을 새로 불러왔습니다.", "info", 120.0
            )

    async def _wait_for_loading(self) -> None:
        # The spinner is gone from the DOM entirely once content arrives, so its
        # absence is not evidence that anything has rendered -- that is what
        # _wait_for_timetable is for. This only smooths over a visible spinner.
        try:
            loader = self._page.locator(".loading_area").first
            if await loader.count() and await loader.is_visible():
                await loader.wait_for(state="hidden", timeout=5000)
        except Exception:
            pass

    async def _login_state(self) -> bool | None:
        try:
            return await self._page.evaluate(LOGIN_STATE_SCRIPT)
        except Exception:
            return None

    async def _ensure_login(self, dev_mode: bool) -> None:
        state = await self._login_state()
        if state:
            self.log("네이버 로그인 상태를 확인했습니다.", "success")
            await self._persist_cookies()
            return

        self.log(
            "⚠️ 네이버 로그인이 필요합니다. 열린 브라우저 창에서 로그인해주세요. "
            "로그인하면 자동으로 이어서 진행합니다.",
            "warning",
        )
        try:
            await self._page.bring_to_front()
        except Exception:
            pass
        try:
            await self._page.goto(
                "https://nid.naver.com/nidlogin.login?url="
                + urllib.parse.quote(self._item_url, safe=""),
                wait_until="domcontentloaded",
            )
        except Exception:
            pass

        deadline = time.monotonic() + self.LOGIN_WAIT_SECONDS
        while time.monotonic() < deadline and not self.stop_event.is_set():
            await asyncio.sleep(2.0)
            url = ""
            try:
                url = self._page.url
            except Exception:
                pass
            if "nidlogin" in url or "nid.naver.com" in url:
                continue
            if "booking.naver.com" not in url:
                continue
            if await self._login_state():
                self.log("✓ 로그인 완료. 예약 감시를 시작합니다.", "success")
                await self._persist_cookies()
                return

        if self.stop_event.is_set():
            return
        raise NaverApiError("로그인 제한시간이 초과되었습니다. 다시 시도해주세요.")

    async def _persist_cookies(self) -> None:
        # Only meaningful for the bundled-browser fallback; the real-Chrome
        # profile already keeps its own cookie jar on disk.
        if not self._owns_browser or self._context is None:
            return
        try:
            self._save_cookies(await self._context.cookies())
        except Exception:
            pass

    async def _teardown_browser(self) -> None:
        # Close the tab we opened. Detaching from a real Chrome leaves everything
        # else alone -- including, before this, the tab this run created. Those
        # accumulated: after roughly ten runs the profile held ten dead booking
        # tabs and connect_over_cdp started taking longer than its 30 s timeout,
        # so the engine hung during setup and never reached the polling loop.
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass

        if self._owns_browser:
            for closer in (self._context, self.browser):
                try:
                    if closer is not None:
                        await closer.close()
                except Exception:
                    continue
        elif self.browser is not None:
            # A CDP close only detaches Playwright; the user's Chrome stays up.
            try:
                await self.browser.close()
            except Exception:
                pass
        self.browser = None
        self._context = None
        self._page = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        if self._chrome_session is not None and self._close_chrome_on_exit:
            self._chrome_session.close_if_launched()
        self._chrome_session = None

        if self.api is not None:
            self.api.close()

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------
    class _Timing:
        """Per-phase stopwatch for the critical path, reported in one line.

        Worth having in the log permanently: the only way to know whether a miss
        was our latency or someone else being earlier is to see where the
        milliseconds went on the run that missed.
        """

        def __init__(self) -> None:
            self.started = time.monotonic()
            self.last = self.started
            self.phases: list[tuple[str, float]] = []

        def mark(self, name: str) -> None:
            now = time.monotonic()
            self.phases.append((name, (now - self.last) * 1000))
            self.last = now

        def summary(self) -> str:
            total = (self.last - self.started) * 1000
            parts = " · ".join(f"{name} {ms:.0f}ms" for name, ms in self.phases)
            return f"총 {total:.0f}ms ({parts})"

    @staticmethod
    async def _poll_for(probe, timeout: float, interval: float):
        """Await ``probe()`` until it returns something truthy, or give up.

        Replaces fixed sleeps in the critical path: a 250 ms sleep costs 250 ms
        even when the answer arrived after 30 ms.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                result = await probe()
            except Exception:
                result = None
            if result:
                return result
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(interval)

    async def _submit(
        self, slot: NaverSlot, reservation_data, dev_mode: bool
    ) -> tuple[str, str]:
        page = self._page
        if page is None:
            return "error", "브라우저가 준비되지 않았습니다"

        self._dialog_state["message"] = ""
        target_date = slot.date_str
        target_minutes = slot.start.hour * 60 + slot.start.minute

        try:
            # Fast path: the page was parked on this date during setup, so if the
            # button is already rendered and free, click it without spending a
            # reload. A reload costs ~2 s of React render, which is the single
            # biggest delay on the critical path.
            state = await self._find_slot_button(
                target_minutes, target_date, reload_first=False)
            if state is None or not (state.get("match") or {}).get("clickable"):
                state = await self._find_slot_button(
                    target_minutes, target_date, reload_first=True)

            rendered = (state or {}).get("rendered", 0)
            match = (state or {}).get("match")

            if not match:
                if dev_mode:
                    await self._dump_debug(page, "naver_timetable_debug.html")
                if not rendered:
                    # A date can exist in the API and still render no timetable.
                    return "notready", (
                        f"{target_date} 시간표가 렌더링되지 않았습니다 "
                        f"(li.time_item 0개)"
                    )
                return "notready", (
                    f"{slot.time_str} 버튼이 화면에 없습니다 "
                    f"(시간표 {rendered}개는 렌더링됨)"
                )
            if not match.get("clickable"):
                # The page is the authority here: the schedule API can report
                # stock while the rendered slot already says 매진.
                return "taken", (
                    f"{slot.time_str} 선택할 수 없는 상태입니다 · "
                    f"화면 표시 {match.get('text', '')!r}"
                )

            timing = self._Timing()
            timing.mark("탐색")

            await page.locator('[data-pengucro-slot="1"]').first.click()

            # Clicking a time does not navigate; the button gains a `selected`
            # class and the 다음 button appears underneath. Measured at ~350 ms,
            # which is React render time and cannot be shortened -- but polling at
            # 25 ms instead of 50 ms stops us overshooting it by up to 50 ms.
            selected = await self._poll_for(
                lambda: page.evaluate(SLOT_SELECTED_SCRIPT), timeout=2.0, interval=0.025
            )
            if not selected:
                return "retry", f"{slot.time_str} 클릭이 반영되지 않았습니다"
            timing.mark("슬롯선택")

            if not await self._click_next():
                if dev_mode:
                    await self._dump_debug(page, "naver_timetable_debug.html")
                return "retry", "'다음' 버튼을 찾지 못했습니다"
            timing.mark("다음클릭")

            if not await self._wait_for_request_page():
                notice = await self._page_notice()
                url = ""
                try:
                    url = page.url
                except Exception:
                    pass
                if "nid.naver.com" in url or "nidlogin" in url:
                    return "login", "네이버 로그인 화면으로 이동했습니다"
                if notice:
                    return self._classify(notice), notice
                if await self._login_state() is False:
                    return "login", "로그인이 만료된 것으로 보입니다"
                if dev_mode:
                    await self._dump_debug(page, "naver_after_next_debug.html")
                return "retry", "예약 정보 입력 화면으로 넘어가지 못했습니다"
            timing.mark("입력화면")

            await self._fill_request_form(reservation_data)
            timing.mark("폼입력")

            # This page carries no checkbox at all: consent is expressed by the
            # submit button's own label. The sweep is kept for businesses that do
            # render checkboxes, and reports nothing when there are none.
            consent = await page.evaluate(CONSENT_SCRIPT)
            if consent and consent.get("total"):
                self.log(
                    f"[정보] 약관 동의 {consent.get('checked')}/{consent.get('total')}개 "
                    f"체크 (클릭: {', '.join(consent.get('clicked') or []) or '없음'})",
                    "info",
                )
                leftover = consent.get("unchecked") or []
                if leftover:
                    self.log(f"[정보] 선택 항목은 그대로 둡니다: {len(leftover)}개", "info")

            # Deliberately not matching a bare "예약하기": the item page carries a
            # tab with exactly that label, so a loose selector can click the tab
            # and report success.
            # button.btn_request, confirmed live. It carries a `disabled` class --
            # not the disabled attribute -- until the required question is answered.
            submit = page.locator(
                'button[class*="btn_request"], button:has-text("동의하고 예약하기"), '
                'a:has-text("동의하고 예약하기")'
            ).first
            try:
                await submit.wait_for(state="visible", timeout=4000)
            except Exception:
                return "retry", "'동의하고 예약하기' 버튼을 찾지 못했습니다"

            async def enabled():
                classes = (await submit.get_attribute("class")) or ""
                return "disabled" not in classes.lower()

            if not await self._poll_for(enabled, timeout=3.0, interval=0.025):
                return "retry", "'동의하고 예약하기' 버튼이 계속 비활성 상태입니다"
            timing.mark("버튼활성")
            self.log(f"[정보] 제출 준비 완료 · {timing.summary()}", "info")

            if dev_mode:
                # The debug dump is deliberately after the timing report so it
                # cannot inflate the measurement of the real critical path.
                await self._dump_debug(page)
                self.log(
                    "[완료] [개발자 테스트] '동의하고 예약하기' 직전에 멈췄습니다. "
                    "제출하지 않습니다.",
                    "success",
                )
                return "dev", ""

            await submit.click()
            self.log("🚀 '동의하고 예약하기' 클릭", "warning")
            return await self._verify_result()

        except Exception as exc:
            if self.stop_event.is_set():
                return "error", "중지됨"
            return "retry", f"{type(exc).__name__}: {str(exc)[:120]}"

    async def _find_slot_button(
        self, target_minutes: int, target_date: str, reload_first: bool
    ):
        """Tag the target time's button. Returns the scan result, or None.

        With ``reload_first`` false this only inspects whatever is already on
        screen, which is what makes the warm-page path cheap.
        """
        if reload_first:
            if not await self._goto_item(
                target_date, timeout_ms=self.TIMETABLE_RETRY_TIMEOUT_MS
            ):
                return {"rendered": 0, "pool": 0, "match": None}
        elif not await self._wait_for_timetable(timeout_ms=1200):
            return None
        try:
            return await self._page.evaluate(SLOT_TAG_SCRIPT, target_minutes)
        except Exception:
            return None

    async def _click_next(self) -> bool:
        try:
            picked = await self._page.evaluate(NEXT_BUTTON_SCRIPT)
        except Exception:
            picked = None
        if not picked:
            return False
        try:
            element = self._page.locator('[data-pengucro-next="1"]').first
            await element.wait_for(state="visible", timeout=4000)
            await element.click()
            return True
        except Exception:
            return False

    async def _wait_for_request_page(self) -> bool:
        # The route is /booking/{type}/bizes/{id}/items/{id}/request -- confirmed
        # from the router table in Naver's own bundle.
        #
        # Without a session, clicking 다음 does nothing at all: the URL stays on
        # the item page and no toast survives long enough to read. So a timeout
        # here most often means the login went stale, which _page_notice and the
        # caller's classification try to distinguish.
        try:
            await self._page.wait_for_url("**/request**", timeout=10000)
            await self._wait_for_loading()
            return True
        except Exception:
            return False

    async def _page_notice(self) -> str:
        """Whatever the page is trying to tell us: toast text, then dialog text.

        Naver surfaces refusals through its own toast component
        (``Toast__inner__*``) rather than a native alert, so the dialog handler
        alone sees nothing.
        """
        try:
            toast = self._page.locator('[class*="Toast__inner"]').first
            if await toast.count():
                text = (await toast.inner_text(timeout=500) or "").strip()
                if text:
                    return text.replace("\n", " ")[:140]
        except Exception:
            pass
        return (self._dialog_state.get("message") or "").strip()[:140]

    async def _fill_request_form(self, reservation_data) -> None:
        page = self._page

        # Reader name and phone come from the Naver account, not from form fields:
        # the live request page renders zero <input> elements. They are only filled
        # if a business actually asks for them.
        name = str(reservation_data.get("name") or "").strip()
        phone = str(reservation_data.get("phone") or "").strip()
        for value, selectors in (
            (name, ['input[name*="name" i]', 'input[placeholder*="이름"]',
                    'input[placeholder*="예약자"]']),
            (phone, ['input[name*="phone" i]', 'input[type="tel"]',
                     'input[placeholder*="연락처"]', 'input[placeholder*="휴대"]']),
        ):
            if not value:
                continue
            for selector in selectors:
                try:
                    field = page.locator(selector).first
                    if not await field.count() or not await field.is_visible(timeout=200):
                        continue
                    if (await field.input_value() or "").strip():
                        break
                    await field.fill(value)
                    break
                except Exception:
                    continue

        if not self._participant:
            return
        title, option_value = self._participant

        # A native <select> is tried first because other businesses may use one.
        try:
            if await page.evaluate(SELECT_OPTION_SCRIPT, option_value):
                self.log(f"[정보] '{title}' → '{option_value}' 선택 완료", "info")
                return
        except Exception:
            pass

        try:
            opened = await page.evaluate(OPEN_SELECT_SCRIPT)
        except Exception:
            opened = None
        if opened is None:
            self.log(f"[경고] '{title}' 선택 컨트롤을 찾지 못했습니다.", "warning")
            return

        # Poll for the option list rather than sleeping a fixed 250 ms: the list
        # is client-side and usually up within a few tens of milliseconds.
        picked = await self._poll_for(
            lambda: page.evaluate(PICK_OPTION_SCRIPT, option_value),
            timeout=2.0, interval=0.02,
        )
        if picked is not True:
            self.log(
                f"[경고] '{option_value}' 선택지를 찾지 못했습니다. "
                f"화면 선택지: {picked}",
                "warning",
            )
            return

        current = await self._poll_for(
            lambda: page.evaluate(SELECTED_OPTION_SCRIPT), timeout=1.0, interval=0.02
        )
        self.log(f"[정보] '{title}' → '{current or option_value}' 선택 완료", "info")

    async def _verify_result(self) -> tuple[str, str]:
        page = self._page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(0.6)

        notice = await self._page_notice()
        url = ""
        try:
            url = page.url
        except Exception:
            pass
        body = ""
        try:
            body = await page.locator("body").inner_text(timeout=2000)
        except Exception:
            pass

        # '/my/' alone used to be treated as success, which any account page would
        # have satisfied. The markers below are specific to a completed booking.
        success_url = ("booking-detail", "/bookings/", "complete", "confirm")
        success_text = ("예약이 완료", "예약되었습니다", "예약 완료", "예약이 접수")
        if any(token in url for token in success_url) or \
                any(token in body for token in success_text):
            return "success", url or "완료 화면 확인"

        if notice:
            return self._classify(notice), notice
        return "retry", f"결과를 확인하지 못했습니다 ({url[:80]})"

    @staticmethod
    def _classify(message: str) -> str:
        text = message or ""
        if any(token in text for token in
               ("이미 예약", "마감", "정원", "매진", "선택할 수 없", "품절")):
            return "taken"
        if any(token in text for token in ("로그인", "인증", "본인확인")):
            return "login"
        return "retry"

    async def _dump_debug(self, page, filename="naver_request_debug.html") -> None:
        try:
            path = data_path(filename)
            with path.open("w", encoding="utf-8") as stream:
                stream.write(await page.content())
            self.log(f"[정보] [디버그] 요청 페이지 HTML 저장: {path}", "info")
        except Exception as exc:
            self.log(f"[경고] 디버그 HTML 저장 실패: {exc}", "warning")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _poll_delay(self, slot: NaverSlot | None, last_change: float) -> tuple[float, str]:
        """How long to wait before the next poll, and a label for the log.

        Three tiers, in priority order:

        ``집중``  A change can land at any instant -- the slot exists but is full
                  (a cancellation frees it immediately), or a known open moment is
                  within two minutes.
        ``기본``  Normal watching speed.
        ``절전``  Nothing has changed for a long time, so an unattended overnight
                  watch stops spending requests at full rate. Any change at all
                  resets ``last_change`` and pulls it back to 기본 on the next turn.
        """
        # The relax check comes first on purpose. It used to sit below the burst
        # check, and since a published-but-full slot always takes the burst branch,
        # a 매진 slot was polled at the burst rate forever -- roughly 4 req/s
        # sustained, which is 350k requests across an unattended night.
        if self._relax_after and (time.monotonic() - last_change) >= self._relax_after:
            return max(self._poll_base, self.POLL_RELAXED_SECONDS), "절전"

        if slot is not None:
            # The slot is published but blocked; whoever holds it can release it
            # without warning.
            return self._poll_burst, "집중"

        open_at = getattr(self, "_open_at_epoch", None)
        if open_at is not None and self.clock is not None:
            remaining = self.clock.seconds_until(open_at)
            if -self.BURST_WINDOW_SECONDS <= remaining <= self.BURST_WINDOW_SECONDS:
                return self._poll_burst, "집중"

        return self._poll_base, "기본"

    @staticmethod
    def _slot_signature(slot: NaverSlot | None, reason: str | None) -> tuple[Any, ...]:
        """What has to change before the state is worth logging again.

        A missing slot gets its own marker rather than ``None`` so the seeded
        starting value can never be mistaken for "not yet observed".
        """
        if slot is None:
            return ("missing",)
        return ("slot", slot.slot_id, slot.stock, slot.remaining, reason)

    @staticmethod
    def _resolve_url(reservation_data) -> str:
        for key in ("themePK", "site_url", "url"):
            value = str(reservation_data.get(key) or "")
            if "booking.naver.com" in value:
                return value
        return ""

    @staticmethod
    def _format_remaining(seconds: float) -> str:
        seconds = max(0, int(seconds))
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        if days:
            return f"{days}일 {hours}시간 {minutes}분"
        if hours:
            return f"{hours}시간 {minutes}분"
        if minutes:
            return f"{minutes}분 {seconds}초"
        return f"{seconds}초"

    def _log_throttled(self, key: str, message: str, level: str, interval: float) -> None:
        now = time.monotonic()
        if now - self._log_marks.get(key, 0.0) < interval:
            return
        self._log_marks[key] = now
        self.log(message, level)

    # -- cookie storage (bundled-browser fallback only) ---------------------
    def _load_cookies(self):
        encrypted = self.secret_store.get("naver_cookies")
        if encrypted:
            try:
                return json.loads(encrypted)
            except (TypeError, ValueError):
                pass

        legacy_path = "naver_cookies.json"
        if os.path.exists(legacy_path):
            try:
                with open(legacy_path, "r", encoding="utf-8") as stream:
                    cookies = json.load(stream)
                self._save_cookies(cookies)
                if self.secret_store.get("naver_cookies"):
                    os.remove(legacy_path)
                return cookies
            except (OSError, TypeError, ValueError):
                pass
        return []

    def _save_cookies(self, cookies):
        self.secret_store.set("naver_cookies", json.dumps(cookies, ensure_ascii=False))
