"""Naver Booking engine: poll and submit through Naver's GraphQL API.

Shape of this engine
--------------------
It keeps trying until the slot is bookable. The loop asks the server "can I book
this yet?" and acts the moment the answer changes. When the product publishes an
explicit opening time, that moment gets a turn of its own: the engine waits on
Naver's synchronized server clock and sends a prepared ``submitBooking`` request
inside the logged-in page context. Supported products therefore avoid the React
render/click round trip; unsupported or rejected API paths retain the browser flow
as a fallback.

That last part is the point. The schedule API reports the slot free before the
opening moment while the page renders no timetable for a date the server has not
opened, so a submit in that window cannot land -- it just burns a full page cycle
(~7 s measured) and comes back "notready", blind the whole time. A run against an
00:00:00 opening spent 23:59:48 and 23:59:56 doing exactly that and so first
looked at the timetable at 00:00:05, by which point the single seat was gone.

The question is asked over GraphQL (see ``engines/naver_api.py``), not by reading
the rendered page. ``hourlySchedule`` answers without a login, returns each slot's
own ``stock``, and -- crucially -- omits dates that have not opened yet. A date
appearing in the response is therefore the open signal, and it shows up
immediately rather than whenever a page reload happens to land at the right
moment.

The mutation was recovered from a lazily loaded booking-page chunk. The engine
recreates the page's current input for the supported non-seat, non-period,
non-Naver-Pay EPISODE shape and calls same-origin ``fetch("/graphql")`` so the
browser retains its cookies, origin and request identity. See
``reference/naver/submit_booking.md`` for the verified schema boundary and the
remaining authenticated end-to-end uncertainty (chiefly ``RT98``, Naver's own
"unusual booking" judgement).

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
import re
import time
import urllib.parse
from typing import Any

from engines.base_engine import BaseEngine
from engines.naver_api import (
    NaverAccount,
    NaverApiError,
    NaverBookingApi,
    NaverServerClock,
    NaverSlot,
    SubmitOutcome,
    parse_ids,
)
from engines.naver_forms import NaverFormAnswer, prepare_custom_form_answers
from engines.naver_submit import (
    NaverBrowserSubmitter,
    NaverSubmitPayloadBuilder,
    NaverSubmitPreparation,
    PAYMENT_BOOKING,
    PAYMENT_NPAY_PREPAID,
    PAYMENT_POSTPAID,
    resolve_booking_quantity,
)
from engines.naver_timing import (
    DEFAULT_TARGET_BEFORE_OPEN_SECONDS,
    NaverTimingProfile,
    load_timing_profile,
    record_timing_observation,
)
from pengucro.diagnostics import format_exception, write_redacted_debug_text
from pengucro.models import parse_bool_flag
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
PICK_OPTION_SCRIPT = r"""(wanted) => {
    const items = Array.from(document.querySelectorAll(
        'button.select_item, button[class*="select_item"], [role="option"]'));
    for (const item of items) {
        const shown = (item.innerText || '').replace(/\s+/g, ' ').trim();
        const wantedText = String(wanted || '').trim();
        const shownDigits = shown.replace(/\D/g, '');
        const wantedDigits = wantedText.replace(/\D/g, '');
        if (shown === wantedText ||
                (wantedDigits && shownDigits === wantedDigits)) {
            item.click();
            return true;
        }
    }
    return items.map(i => (i.innerText || '').trim()).slice(0, 12);
}"""

CUSTOM_FORM_CONTROL_SCRIPT = r"""(payload) => {
    const norm = value => String(value || '').replace(/\s+/g, '').toLowerCase();
    const title = norm(payload.title);
    const headings = Array.from(document.querySelectorAll(
        '.form_title, label.form_title, [class*="form_title"]'));
    const matches = headings.filter(el => {
        const own = Array.from(el.childNodes)
            .filter(node => node.nodeType === Node.TEXT_NODE)
            .map(node => node.textContent || '').join('');
        const text = norm(own || el.innerText || '');
        return text === title || text.startsWith(title) || title.startsWith(text);
    });
    const heading = matches[Math.max(0, Number(payload.occurrence) || 0)] || null;
    let scope = heading && (heading.parentElement || heading);
    if (!scope) {
        const direct = document.querySelector(
            '#extra' + payload.index + ', [name="extra' + payload.index + '"]');
        scope = direct && (direct.parentElement || direct);
    }
    if (!scope) return {state: 'missing', title: payload.title};

    const kind = String(payload.kind || '').toUpperCase();
    const values = Array.isArray(payload.values) ? payload.values : [];
    if (kind === 'CHECKBOX') {
        let checked = 0;
        for (const box of scope.querySelectorAll('input[type="checkbox"]')) {
            const label = box.closest('label') || box.parentElement;
            const labelText = norm(label && label.innerText);
            const wanted = values.some(value => labelText.includes(norm(value)));
            if (wanted && !box.checked) box.click();
            if (!wanted && box.checked) box.click();
            if (box.checked) checked += 1;
        }
        return {state: checked ? 'filled' : 'missing', checked};
    }

    if (kind === 'TEXT' || kind === 'TEXTAREA' ||
            !['SELECT', 'RADIO', 'GENDER', 'BIRTH'].includes(kind)) {
        const field = scope.querySelector('input:not([type="checkbox"]):not([type="radio"]), textarea') ||
            document.querySelector('#extra' + payload.index + ', [name="extra' + payload.index + '"]');
        if (!field) return {state: 'missing'};
        const prototype = field.tagName === 'TEXTAREA' ?
            HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
        setter.call(field, String(payload.value || ''));
        field.dispatchEvent(new Event('input', {bubbles: true}));
        field.dispatchEvent(new Event('change', {bubbles: true}));
        return {state: 'filled', value: field.value};
    }

    const selects = Array.from(scope.querySelectorAll('select'));
    if (selects.length) {
        const select = selects[Math.min(Number(payload.part) || 0, selects.length - 1)];
        const wanted = norm(values[Number(payload.part) || 0] || payload.value);
        const option = Array.from(select.options).find(option => {
            const shown = norm(option.textContent);
            const shownDigits = shown.replace(/\D/g, '');
            const wantedDigits = wanted.replace(/\D/g, '');
            return shown === wanted || (wantedDigits && shownDigits === wantedDigits);
        });
        if (!option) return {state: 'missing'};
        select.value = option.value;
        select.dispatchEvent(new Event('input', {bubbles: true}));
        select.dispatchEvent(new Event('change', {bubbles: true}));
        return {state: 'filled', value: option.textContent || option.value};
    }

    const triggers = Array.from(scope.querySelectorAll(
        'button.select_btn, button[class*="select_btn"]'));
    const trigger = triggers[Number(payload.part) || 0];
    if (!trigger) return {state: 'missing', triggers: triggers.length};
    trigger.click();
    return {state: 'opened', current: (trigger.innerText || '').trim()};
}"""

# Quantity is a separate control on performance/ticket products.  It appears on
# the timetable after a slot is selected and before the "다음" button.  Hashed
# class suffixes change, but the stable Count component prefixes and plus/minus
# button names are shared across Naver performance products.
BOOKING_QUANTITY_SCRIPT = r"""(action) => {
    const controls = Array.from(document.querySelectorAll(
        'div[class*="count_control"], div[class*="Count__count_control"]'))
        .filter(group => group.querySelector(
            'button[class*="btn_plus"], button[data-click-code*="plusbookingcount"]'));
    if (!controls.length) return {present: false};
    const read = group => {
        const number = group.querySelector(
            'span[class*="Count__num"], span[class*="count_num"]');
        const current = Number((number && number.textContent || '').replace(/\D/g, ''));
        const plus = group.querySelector(
            'button[class*="btn_plus"], button[data-click-code*="plusbookingcount"]');
        const minus = group.querySelector(
            'button[class*="btn_minus"], button[data-click-code*="minusbookingcount"]');
        const disabled = button => !button || button.disabled ||
            button.getAttribute('aria-disabled') === 'true' ||
            /disabled/i.test(button.className || '');
        return {group, current, plus, minus,
                plusDisabled: disabled(plus), minusDisabled: disabled(minus)};
    };
    const states = controls.map(read);
    const selected = states.find(state => state.current > 0) || states[0];
    if (!action || action === 'state') {
        return {present: true, current: selected.current,
                plusDisabled: selected.plusDisabled,
                minusDisabled: selected.minusDisabled,
                groups: states.length};
    }
    const button = action === 'plus' ? selected.plus : selected.minus;
    const isDisabled = action === 'plus' ?
        selected.plusDisabled : selected.minusDisabled;
    if (!button || isDisabled) {
        return {present: true, current: selected.current, disabled: true,
                groups: states.length};
    }
    button.click();
    return {present: true, current: selected.current, clicked: true,
            groups: states.length};
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

# Account state is published in the page's own Apollo cache, so login can be
# confirmed from a page we were going to load anyway.
LOGIN_STATE_SCRIPT = r"""() => {
    const state = window.__APOLLO_STATE__ || {};
    let found = null;
    for (const key of Object.keys(state)) {
        const entry = state[key];
        if (!entry || entry.__typename !== 'Account') continue;
        found = found || !!entry.isLoggedIn;
        if (entry.isLoggedIn) return true;
    }
    return found;
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
    OPEN_SCHEDULE_REFRESH_FAR_SECONDS = 300.0
    OPEN_SCHEDULE_REFRESH_NEAR_SECONDS = 30.0
    OPEN_SCHEDULE_NEAR_WINDOW_SECONDS = 3600.0
    OPEN_SCHEDULE_FINAL_REFRESH_LEAD = 30.0

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
    # No page reload is started inside this window before the opening moment. A
    # reload blocks the loop for a couple of seconds and cannot produce a
    # timetable for a date that has not opened, so one landing across the boundary
    # would delay the only reload that matters.
    OPEN_BLACKOUT_SECONDS = 20.0
    # How early the loop hands the turn over to the strike routine, which then
    # waits on the server clock itself. Wide enough to absorb one API round trip
    # plus the clock's own ~±45 ms uncertainty.
    OPEN_ARM_SECONDS = 5.0
    # The date only starts rendering once the server has opened it, so the reload
    # is fired at the boundary and retried if React comes back with nothing.
    OPEN_RELOAD_ATTEMPTS = 3
    OPEN_RELOAD_TIMEOUT_MS = 7000
    API_SUBMIT_MAX_ATTEMPTS = 3
    API_NOT_OPEN_WINDOW_SECONDS = 0.35
    API_NOT_OPEN_RETRY_SECONDS = 0.01
    API_PREFLIGHT_MIN_SECONDS = 2.0
    API_PREFLIGHT_SLOT_TIMEOUT_SECONDS = 0.75
    # Install one same-origin browser timer before the boundary so the final
    # booking fetch does not wait for a Python -> CDP wake-up at open time.
    API_BROWSER_ARM_MIN_SECONDS = 0.30
    API_BROWSER_ARM_FINAL_QUIET_SECONDS = 0.10
    API_BROWSER_ARM_STATUS_SECONDS = 0.025
    # Postpaid starts at 60 ms before open; prepaid uses a safer 20 ms product
    # profile because its gate can lag the published boundary. The browser flow
    # retries only an explicit NOT_OPEN response and never treats RT47 as
    # permission to repost.
    API_SEND_MIN_LEAD_SECONDS = 0.020
    API_SEND_MAX_LEAD_SECONDS = 0.250
    API_PREOPEN_CLOCK_SAMPLES = 5
    API_REFUSED_RECHECK_TIMEOUT_SECONDS = 0.30
    API_RECONCILE_ATTEMPTS = 120
    API_RECONCILE_WINDOW_SECONDS = 60.0
    API_POST_SUBMIT_INVENTORY_OFFSETS = (0.0, 0.10, 0.30, 1.00)
    # Naver documents that a configured opening can be applied minutes late.
    # After an explicit NOT_OPEN response, watch the rendered timetable without
    # another mutation. Keep an intensive five-minute watch, then continue at a
    # low rate instead of abandoning a genuinely delayed opening.
    API_DELAYED_OPEN_ACTIVE_WINDOW_SECONDS = 300.0
    API_DELAYED_OPEN_ACTIVE_POLL_SECONDS = 0.25
    API_DELAYED_OPEN_SLOW_POLL_SECONDS = 5.0
    API_DELAYED_OPEN_PAGE_TIMEOUT_MS = 4000
    # After the page reports it has nothing to click, wait this long before
    # driving it again. API polling is unaffected.
    NOTREADY_BACKOFF_SECONDS = 1.5

    SUBMIT_MAX_ATTEMPTS = 10
    PAGE_TIMEOUT_MS = 15000
    NAVIGATION_TIMEOUT_MS = 20000
    LOGIN_WAIT_SECONDS = 300.0
    CDP_CONNECT_TIMEOUT_MS = 12000
    NPAY_PAGE_TIMEOUT_SECONDS = 20.0
    NPAY_CONTROL_TIMEOUT_SECONDS = 15.0
    # Locator.click() otherwise inherits Playwright's 30-second default.  The
    # checkout controls are retried by our own loop, so a short per-action budget
    # keeps the GUI stop button responsive even while Npay is still rendering.
    NPAY_ACTION_TIMEOUT_MS = 750
    NPAY_MONITOR_INTERVAL_SECONDS = 0.25
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
        self._custom_form_answers: list[NaverFormAnswer] = []
        self._item_url = ""
        self._log_marks: dict[str, float] = {}
        self._dialog_state: dict[str, str] = {"message": ""}
        self._open_at_epoch: float | None = None
        self._open_strike_pending = False
        self._uses_open_schedule = False
        self._last_open_schedule_refresh = 0.0
        self._final_open_schedule_refresh = False
        self._final_clock_sync_attempted = False
        self._notready_until = 0.0
        self._last_warm = 0.0
        self._warmed_for_date = False
        self._api_submitter: NaverBrowserSubmitter | None = None
        self._api_preparation: NaverSubmitPreparation | None = None
        self._api_submit_enabled = False
        self._api_submit_blocked = False
        self._api_submit_state = "idle"
        self._api_prepare_pending = False
        self._api_refused_signature: tuple[Any, ...] | None = None
        self._api_delayed_open_started = 0.0
        self._api_delayed_open_slow_logged = False
        self._api_account: NaverAccount | None = None
        self._api_business: dict[str, Any] | None = None
        self._api_biz_item: dict[str, Any] | None = None
        self._slot_post_payment: dict[str, bool] = {}
        self._api_payment_signature: tuple[str, str, bool] | None = None
        self._npay_booking_id = ""
        self._recent_submit_rtt: list[float] = []
        self._last_api_lead_detail = ""
        self._timing_profile = NaverTimingProfile(
            "", DEFAULT_TARGET_BEFORE_OPEN_SECONDS, 0
        )
        self._timing_profile_log_signature: tuple[str, float, int] | None = None
        self._last_post_submit_inventory: list[dict[str, Any]] = []

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
        self._api_submitter = None
        self._api_preparation = None
        self._api_submit_enabled = False
        self._api_submit_blocked = False
        self._api_submit_state = "idle"
        self._api_prepare_pending = False
        self._api_refused_signature = None
        self._api_delayed_open_started = 0.0
        self._api_delayed_open_slow_logged = False
        self._api_account = None
        self._api_business = None
        self._api_biz_item = None
        self._slot_post_payment = {}
        self._api_payment_signature = None
        self._npay_booking_id = ""
        self._timing_profile = NaverTimingProfile(
            "", DEFAULT_TARGET_BEFORE_OPEN_SECONDS, 0
        )
        self._timing_profile_log_signature = None
        self._last_post_submit_inventory = []
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
        try:
            self._api_biz_item = await asyncio.to_thread(
                self.api.fetch_biz_item_raw
            )
        except Exception:
            self._api_biz_item = None
        if self._api_biz_item:
            self._log_item_payment_preview(self._api_biz_item)
        else:
            self.log(
                "[정보] 예약 시작 전 결제 방식 확인 대기 · 상품 정보를 아직 "
                "받지 못해 대상 슬롯 공개 후 다시 판별합니다.",
                "info",
            )
        await asyncio.to_thread(self.clock.sync, True)

        blocked = meta.hard_block()
        if blocked:
            raise NaverApiError(blocked)

        target_open_at = await asyncio.to_thread(
            self.api.resolve_target_open_at, target_date, meta
        )
        # The engine never sleeps until this time: API polling continues. The
        # synchronized server clock drives the API-first strike at the opening
        # boundary; the browser is only the fallback when direct submission is
        # unavailable for that product/session.
        self._open_at_epoch = (
            target_open_at.timestamp()
            if (meta.uses_open_schedule and target_open_at) else None
        )
        self._uses_open_schedule = bool(meta.uses_open_schedule)
        self._last_open_schedule_refresh = time.monotonic()
        self._final_open_schedule_refresh = False
        self._final_clock_sync_attempted = False
        self._open_strike_pending = False
        if meta.uses_open_schedule and target_open_at:
            remaining = self.clock.seconds_until(target_open_at.timestamp())
            if remaining > 0:
                self._open_strike_pending = True
                self.log(
                    f"[정보] {target_date} 예약 오픈 예정 "
                    f"{target_open_at:%Y-%m-%d %H:%M} · "
                    f"서버 시간 기준 {self._format_remaining(remaining)} 남음 · "
                    "오픈 시각에 API 직접 제출을 우선하고, 준비되지 않은 경우에만 "
                    "예약 화면으로 제출합니다.",
                    "info",
                )
            else:
                self.log(
                    f"[정보] {target_date} 예약 오픈 "
                    f"{target_open_at:%Y-%m-%d %H:%M} (이미 지남)", "info"
                )

        if self._api_business is None:
            business = await asyncio.to_thread(self.api.fetch_business)
            self._api_business = business or None
        business_form = (
            (self._api_business or {}).get("customFormJson")
            if self._api_business else []
        )
        form = meta.custom_form or (
            business_form if isinstance(business_form, list) else []
        )
        business_type_id = int(
            (self._api_business or {}).get("businessTypeId") or 0
        )
        form_item_count = (
            int(re.sub(r"\D", "", str(reservation_data.get("people") or "")) or 1)
            if business_type_id and business_type_id != 12
            else 1
        )
        _prepared_form, self._custom_form_answers, form_error = (
            prepare_custom_form_answers(
                form,
                reservation_data,
                item_count=form_item_count,
            )
        )
        if form_error:
            raise NaverApiError(form_error)
        if self._custom_form_answers:
            self.log(
                f"[정보] 동적 추가정보 {len(self._custom_form_answers)}개 자동 준비 완료 · "
                "문항 수와 유형을 서버 폼 기준으로 처리합니다.",
                "info",
            )
            for answer in self._custom_form_answers:
                item = f" · {answer.item_order}번째 관람자" if answer.item_order else ""
                sensitive = re.search(
                    r"이름|성명|연락처|전화|휴대폰|이메일|생년월일|birth|phone|name|email",
                    answer.title,
                    re.I,
                )
                shown_value = "개인정보 입력 완료" if sensitive else answer.value
                self.log(
                    f"[정보] 추가 입력 '{answer.title}'{item} → "
                    f"'{shown_value}' ({answer.strategy})",
                    "info",
                )

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
        await self._prepare_api_submit(
            reservation_data,
            dev_mode=parse_bool_flag(reservation_data.get("devMode", False)),
        )

    async def _augment_slot_payment(
        self, slot: dict[str, Any] | None, *, timeout: float | None = None
    ) -> dict[str, Any] | None:
        """Attach the official selected-slot pre/post-payment flag once.

        The public hourly schedule does not include this field.  Naver's own page
        immediately follows a time selection with the read-only ``Slot`` query,
        so doing the same during preparation lets us classify payment before the
        opening boundary without rendering or clicking the timetable.
        """
        if not isinstance(slot, dict) or not isinstance(self._api_biz_item, dict):
            return slot
        if not parse_bool_flag(self._api_biz_item.get("isNPayUsed")):
            return slot
        slot_id = str(slot.get("slotId") or "")
        if not slot_id or self.api is None:
            return slot

        resolved = self._slot_post_payment.get(slot_id)
        if resolved is None:
            fetcher = getattr(self.api, "fetch_slot_post_payment", None)
            if not callable(fetcher):
                return slot
            try:
                task = asyncio.to_thread(fetcher, slot_id)
                value = (
                    await asyncio.wait_for(task, timeout=timeout)
                    if timeout is not None
                    else await task
                )
            except Exception:
                value = None
            if value is None:
                return slot
            resolved = bool(value)
            self._slot_post_payment[slot_id] = resolved

        augmented = dict(slot)
        augmented["_isPostPaymentResolved"] = True
        augmented["isPostPayment"] = resolved
        return augmented

    def _log_item_payment_preview(self, biz_item: dict[str, Any]) -> None:
        """Show a product-level payment type even before the target slot exists."""
        if not parse_bool_flag(biz_item.get("isNPayUsed")):
            mode = PAYMENT_BOOKING
            source = "bizItem.isNPayUsed=false"
        else:
            raw_setting = biz_item.get("paymentSettingJson")
            if isinstance(raw_setting, dict):
                payment_setting = raw_setting
            elif isinstance(raw_setting, str):
                try:
                    parsed = json.loads(raw_setting)
                except (TypeError, ValueError):
                    parsed = {}
                payment_setting = parsed if isinstance(parsed, dict) else {}
            else:
                payment_setting = {}
            payment_moment = str(
                payment_setting.get("paymentMoment") or ""
            ).upper()
            mode = (
                PAYMENT_POSTPAID
                if payment_moment == "POST"
                else PAYMENT_NPAY_PREPAID
            )
            source = (
                "bizItem.paymentSettingJson.paymentMoment"
                if payment_moment
                else "bizItem.isNPayUsed=true·네이버 기본값(선결제)"
            )

        self._log_payment_preparation(
            NaverSubmitPreparation(
                False,
                payment_mode=mode,
                payment_source=source,
            ),
            provisional=True,
        )

    def _log_payment_preparation(
        self,
        preparation: NaverSubmitPreparation,
        *,
        provisional: bool = False,
    ) -> None:
        signature = (
            preparation.payment_mode,
            preparation.payment_source,
            provisional,
        )
        if signature == self._api_payment_signature:
            return
        self._api_payment_signature = signature
        if provisional:
            suffix = (
                "상품 정보 기준 1차 판별입니다. 대상 슬롯이 공개되면 "
                "슬롯 정보로 최종 확인합니다."
            )
        else:
            suffix = (
                "API 선점 후 Npay 결제 화면으로 즉시 이동합니다."
                if preparation.requires_checkout
                else "예약번호 생성까지 API로 즉시 처리합니다."
            )
        self.log(
            f"[정보] 예약 시작 전 결제 방식 확인 · {preparation.payment_label} · "
            f"근거 {preparation.payment_source or '상품 메타데이터'} · {suffix}",
            "success",
        )

    def _api_may_submit(self, dev_mode: bool) -> bool:
        if not self._api_submit_enabled or self._api_preparation is None:
            return False
        return not dev_mode or self._api_preparation.requires_checkout

    def _adopt_timing_profile(self, preparation: NaverSubmitPreparation) -> None:
        payload = preparation.payload if isinstance(preparation.payload, dict) else {}
        profile = load_timing_profile(
            str(payload.get("businessId") or ""),
            str(payload.get("bizItemId") or ""),
            preparation.payment_mode,
        )
        self._timing_profile = profile
        signature = (
            profile.key,
            profile.target_before_open_seconds,
            profile.observation_count,
        )
        if not profile.key or profile.observation_count <= 0:
            return
        if signature == self._timing_profile_log_signature:
            return
        self._timing_profile_log_signature = signature
        self.log(
            f"[정보] 상품별 선점 타이밍 학습 적용 · 실측 {profile.observation_count}회 · "
            f"서버 도착 목표 -{profile.target_before_open_seconds * 1000:.0f}ms",
            "success",
        )

    async def _prepare_api_submit(self, reservation_data, dev_mode: bool) -> None:
        """Prepare the supported direct mutation without exposing its secrets."""
        if self._api_submit_blocked:
            return
        self._api_submit_enabled = False
        self._api_preparation = None
        self._api_prepare_pending = False
        if self.api is None or self._page is None:
            self.log(
                "[경고] API 직접 제출 준비 실패 · 브라우저 제출로 진행합니다.",
                "warning",
            )
            return

        submitter = self._api_submitter or NaverBrowserSubmitter(self._page)
        self._api_submitter = submitter
        if (
            self._api_account is None
            or not self._api_account.is_logged_in
            or not self._api_account.csrf_token
        ):
            account = await submitter.fetch_account()
            if account.is_logged_in and account.csrf_token:
                self._api_account = account
        target_date = str(reservation_data.get("reservationDate") or "")
        target_time = str(reservation_data.get("reservationTime") or "")[:5]
        try:
            # ``requests.Session`` is not a concurrent client. These reads happen
            # well before opening, so keep them sequential and leave ``last_rtt``
            # describing the final slot request used by the send-time estimate.
            if self._api_business is None:
                business = await asyncio.to_thread(
                    self.api.fetch_business
                )
                if business:
                    self._api_business = business
            if self._api_biz_item is None:
                biz_item = await asyncio.to_thread(
                    self.api.fetch_biz_item_raw
                )
                if biz_item:
                    self._api_biz_item = biz_item
            slot = await asyncio.to_thread(
                self.api.fetch_slot_raw, target_date, target_time
            )
            slot = await self._augment_slot_payment(slot)
        except Exception as exc:
            self.log(
                f"[경고] API 직접 제출 준비 실패 ({type(exc).__name__}) · "
                "브라우저 제출로 진행합니다.",
                "warning",
            )
            return

        if (
            self._api_account is None
            or not self._api_account.is_logged_in
            or not self._api_account.csrf_token
            or self._api_business is None
            or self._api_biz_item is None
        ):
            self._api_prepare_pending = True
            self._api_preparation = NaverSubmitPreparation(
                False,
                reason="로그인 또는 상품 상세 조회가 일시적으로 완료되지 않았습니다",
            )
            self.log(
                "[정보] API 직접 제출 준비 대기 · 로그인·상품 정보를 "
                "다시 확인합니다.",
                "info",
            )
            return

        if slot is None:
            self._api_prepare_pending = True
            self._api_preparation = NaverSubmitPreparation(
                False,
                reason="대상 슬롯 상세 정보가 아직 공개되지 않았습니다",
            )
            self.log(
                "[정보] API 직접 제출 준비 대기 · 대상 슬롯이 공개되면 "
                "다시 준비합니다.",
                "info",
            )
            return

        quantity = resolve_booking_quantity(slot, reservation_data)
        item_form = (self._api_biz_item or {}).get("customFormJson")
        business_form = (self._api_business or {}).get("customFormJson")
        form = (
            item_form
            if isinstance(item_form, list) and item_form
            else business_form if isinstance(business_form, list) else []
        )
        _prepared_form, refreshed_answers, form_error = prepare_custom_form_answers(
            form,
            reservation_data,
            item_count=quantity.count,
        )
        if not form_error:
            self._custom_form_answers = refreshed_answers

        preparation = NaverSubmitPayloadBuilder().prepare(
            business=self._api_business or {},
            biz_item=self._api_biz_item or {},
            slot=slot,
            account=self._api_account,
            reservation=reservation_data,
        )
        self._api_preparation = preparation
        if not preparation.ready:
            self._api_submit_blocked = True
            self.log(
                f"[정보] API 직접 제출 준비 안 됨 · {preparation.reason} · "
                "브라우저 제출로 진행합니다.",
                "info",
            )
            return
        self._adopt_timing_profile(preparation)
        self._log_payment_preparation(preparation)
        if preparation.quantity_mode:
            self.log(
                f"[정보] 수량 선택형 예매 확인 · 티켓 "
                f"{preparation.payload.get('bookingCount')}매 · 예상 결제금액 "
                f"{int(preparation.payload.get('price') or 0):,}원"
                + (
                    f" · 현재 잔여 {preparation.available_count}매"
                    if preparation.available_count is not None else ""
                ),
                "success",
            )
        if dev_mode and not preparation.requires_checkout:
            field_names = ", ".join(sorted(preparation.payload))
            self.log(
                f"[정보] 개발자 테스트 · API 페이로드 검증 완료 · "
                f"slotId={preparation.slot_id} · "
                f"필드 {len(preparation.payload)}개: {field_names} · "
                "실제 제출하지 않습니다.",
                "info",
            )
            return

        self._api_submit_enabled = True
        if dev_mode:
            self.log(
                f"[정보] 개발자 테스트 · Npay API 임시 선점 준비 완료 · "
                f"slotId={preparation.slot_id} · 오픈 순간 새로고침 없이 선점한 뒤 "
                "최종 결제 직전에 멈춥니다.",
                "warning",
            )
        else:
            self.log(
                f"[정보] API 직접 제출 준비 완료 · slotId={preparation.slot_id} · "
                "오픈 순간 페이지 새로고침 없이 제출합니다.",
                "success",
            )

    async def _refresh_api_submit(self, reservation_data) -> bool:
        """Refresh volatile account/slot data without discarding a ready payload."""
        if (
            self.api is None
            or self._api_submitter is None
            or self._api_preparation is None
            or not self._api_preparation.ready
            or self._api_business is None
            or self._api_biz_item is None
        ):
            return False

        target_date = str(reservation_data.get("reservationDate") or "")
        target_time = str(reservation_data.get("reservationTime") or "")[:5]

        async def fetch_slot():
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self.api.fetch_slot_raw, target_date, target_time
                ),
                timeout=self.API_PREFLIGHT_SLOT_TIMEOUT_SECONDS,
            )

        account_result, slot_result = await asyncio.gather(
            self._api_submitter.fetch_account(),
            fetch_slot(),
            return_exceptions=True,
        )
        account = (
            account_result
            if isinstance(account_result, NaverAccount)
            and account_result.is_logged_in
            and account_result.csrf_token
            else self._api_account
        )
        slot = slot_result if isinstance(slot_result, dict) else None
        if slot is not None:
            slot = await self._augment_slot_payment(
                slot, timeout=self.API_PREFLIGHT_SLOT_TIMEOUT_SECONDS
            )
        if account is None or slot is None:
            self.log(
                "[정보] API 오픈 직전 갱신이 지연되어 기존 검증값을 사용합니다.",
                "info",
            )
            return False

        preparation = NaverSubmitPayloadBuilder().prepare(
            business=self._api_business,
            biz_item=self._api_biz_item,
            slot=slot,
            account=account,
            reservation=reservation_data,
        )
        if not preparation.ready:
            self.log(
                "[정보] API 오픈 직전 갱신값을 사용할 수 없어 기존 검증값을 "
                "유지합니다.",
                "info",
            )
            return False

        changed = (
            preparation.slot_id != self._api_preparation.slot_id
            or preparation.payload.get("csrfToken")
            != self._api_preparation.payload.get("csrfToken")
            or preparation.payload.get("price")
            != self._api_preparation.payload.get("price")
            or preparation.payment_mode != self._api_preparation.payment_mode
        )
        self._adopt_browser_account(account, invalidate_preparation=False)
        self._api_preparation = preparation
        self._adopt_timing_profile(preparation)
        self._log_payment_preparation(preparation)
        if changed:
            self.log(
                f"[정보] API 오픈 직전 갱신 완료 · slotId={preparation.slot_id}",
                "success",
            )
        return True

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------
    async def make_reservation_async_task(self, reservation_data, task_idx):
        target_date = str(reservation_data.get("reservationDate") or "")
        target_time = str(reservation_data.get("reservationTime") or "")[:5]
        dev_mode = parse_bool_flag(reservation_data.get("devMode", False))
        assert self.api is not None and self.clock is not None

        last_resync = time.monotonic()
        submit_attempts = 0
        last_signature: tuple[Any, ...] | None = getattr(self, "_last_signature", None)
        last_change = time.monotonic()

        while not self.stop_event.is_set():
            until_open = self._seconds_until_open()
            if self._uses_open_schedule:
                near_open = (
                    until_open is not None
                    and abs(until_open) <= self.OPEN_SCHEDULE_NEAR_WINDOW_SECONDS
                )
                refresh_interval = (
                    self.OPEN_SCHEDULE_REFRESH_NEAR_SECONDS
                    if near_open else self.OPEN_SCHEDULE_REFRESH_FAR_SECONDS
                )
                final_due = (
                    until_open is not None
                    and self.OPEN_BLACKOUT_SECONDS < until_open
                    <= self.OPEN_SCHEDULE_FINAL_REFRESH_LEAD
                    and not self._final_open_schedule_refresh
                )
                periodic_due = (
                    time.monotonic() - self._last_open_schedule_refresh
                    >= refresh_interval
                )
                outside_refresh_blackout = (
                    until_open is None
                    or until_open > self.OPEN_BLACKOUT_SECONDS
                    or until_open < -self.OPEN_BLACKOUT_SECONDS
                )
                if final_due or (periodic_due and outside_refresh_blackout):
                    refreshed = await self._refresh_open_schedule(target_date)
                    if final_due and refreshed is not None:
                        self._final_open_schedule_refresh = True
                    if final_due:
                        await self._sync_clock_before_open()
                    until_open = self._seconds_until_open()
            outside_blackout = (
                until_open is None
                or until_open > self.OPEN_BLACKOUT_SECONDS
                or until_open < -self.OPEN_BLACKOUT_SECONDS
            )
            if (
                outside_blackout
                and time.monotonic() - last_resync >= self.CLOCK_RESYNC_SECONDS
            ):
                last_resync = time.monotonic()
                await asyncio.to_thread(self.clock.sync, False)

            # Seconds of server time until the published opening moment. None when
            # the item publishes none, in which case nothing below gates on it.
            until_open = self._seconds_until_open()

            # The opening boundary is handled by one dedicated turn that owns both
            # the reload and the submit. Everything else -- API polling included --
            # steps aside for it, because the run is decided in the second or two
            # after the boundary and any other work in flight is time spent blind.
            if self._claim_open_strike(until_open):
                if not self.submission_lock.acquire(blocking=False):
                    # Give the claim back. Nothing ran, and the boundary turn is
                    # the one thing in this engine that must not be dropped
                    # because a lock happened to be held for an instant.
                    self._open_strike_pending = True
                    await asyncio.sleep(0.01)
                    continue
                try:
                    outcome, detail = await self._strike_at_open(
                        target_date, target_time, reservation_data, dev_mode
                    )
                finally:
                    try:
                        self.submission_lock.release()
                    except RuntimeError:
                        pass
                if outcome == "retry":
                    submit_attempts += 1
            else:
                try:
                    slot = await asyncio.to_thread(
                        self.api.find_slot, target_date, target_time
                    )
                except NaverApiError as exc:
                    self.silent_tick(f"{target_time} 조회 실패")
                    self._log_throttled(
                        "poll_error", f"[경고] 조회 실패: {exc}", "warning", 15.0
                    )
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
                    if (
                        self._api_refused_signature is not None
                        and signature != self._api_refused_signature
                    ):
                        self._api_refused_signature = None
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
                self._last_signature = signature

                if self._api_prepare_pending and slot is not None:
                    await self._prepare_api_submit(reservation_data, dev_mode)

                if slot is None or reason is not None:
                    # Keep the parked tab useful. A date that has not opened renders
                    # no timetable at all, so the warm-click path has nothing to
                    # click until the page is reloaded *after* the date appears.
                    # Reload once on that transition, and periodically so a tab left
                    # for hours is not stale when it finally matters.
                    await self._rewarm_if_needed(
                        target_date, slot is not None, until_open
                    )
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

                # ---- bookable per the API, but the page may not agree yet ----
                #
                # Before the opening moment the schedule API happily reports the
                # slot free while the page renders no timetable at all, so a submit
                # cannot possibly land: it burns a full page cycle (~7 s measured)
                # and returns "notready". Two of those either side of midnight is
                # what made the engine reach an 00:00:00 opening at 00:00:05, by
                # which time the single seat was gone. So wait on the server clock
                # and let the strike turn above do the work.
                if until_open is not None and until_open > 0:
                    self.silent_tick(f"{target_time} 오픈 대기")
                    self._log_throttled(
                        "preopen",
                        f"[정보] {target_date} {target_time} 예약 가능 표시 · "
                        f"오픈까지 {self._format_remaining(until_open)} 남아 화면이 "
                        "아직 열리지 않았습니다 · 오픈 시각에 바로 제출합니다.",
                        "info",
                        30.0,
                    )
                    await asyncio.sleep(
                        min(self._poll_base,
                            max(0.05, until_open - self.OPEN_ARM_SECONDS))
                    )
                    continue

                if self._api_refused_signature == signature:
                    self.silent_tick(f"{target_time} 서버 거절 상태 확인 중")
                    await asyncio.sleep(self._poll_burst)
                    continue

                # The page just told us it has nothing to click. Retrying instantly
                # costs another page cycle for the same answer, so keep polling the
                # API -- which is free and faster -- until the backoff expires.
                if time.monotonic() < self._notready_until:
                    self.silent_tick(f"{target_time} 화면 준비 대기")
                    await asyncio.sleep(self._poll_burst)
                    continue

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
                        f"{target_time} 예약 가능 확인 (잔여 {slot.remaining}) · "
                        "제출을 시작합니다.",
                        "warning",
                    )
                    if self._api_may_submit(dev_mode):
                        outcome, detail = await self._submit_api_first(
                            signature,
                            reservation_data=reservation_data,
                            dev_mode=dev_mode,
                        )
                        if outcome == "delayed_open":
                            outcome, detail = await self._wait_for_delayed_api_open(
                                target_date,
                                target_time,
                                reservation_data,
                                dev_mode,
                            )
                        if outcome == "fallback":
                            outcome, detail = await self._submit(
                                target_date,
                                target_time,
                                reservation_data,
                                dev_mode,
                            )
                    else:
                        outcome, detail = await self._submit(
                            target_date, target_time, reservation_data, dev_mode
                        )
                    # Only our own breakage counts against the budget.
                    #
                    # "notready" means the page had not rendered, and "taken" means
                    # the page says 매진 -- neither is a failed booking attempt, and
                    # neither should end a watch that is meant to run for hours.
                    # Counting them made the engine give up after fifteen seconds
                    # whenever the schedule API ran briefly ahead of the page.
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
            if outcome == "stopped" or self.stop_event.is_set():
                return
            if outcome == "dev":
                while not self.stop_event.is_set():
                    await asyncio.sleep(0.5)
                return
            if outcome == "payment":
                self.log(f"[정보] {detail}", "success")
                completion = await self._monitor_npay_completion()
                if completion:
                    self.log(f"🎉 네이버 예약 결제 완료! {completion}", "success")
                    self.notify_success()
                return
            if outcome == "unknown":
                self.log(
                    f"[경고] 제출 결과를 확인할 수 없습니다. {detail} "
                    "중복 제출을 막기 위해 자동 시도를 중지합니다.",
                    "warning",
                )
                return
            if outcome == "duplicate":
                self.log(
                    f"[경고] 네이버가 동일 계정의 중복 예약으로 응답했습니다. "
                    f"{detail} · 추가 제출은 하지 않습니다. 네이버 예약내역에서 "
                    "접수 여부를 확인해주세요.",
                    "warning",
                )
                return
            if outcome == "notready":
                # Hold the page off for a moment: the API poll keeps running at
                # full speed and will tell us as much as another reload would.
                self._notready_until = time.monotonic() + self.NOTREADY_BACKOFF_SECONDS
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
                if await self._refused_slot_changed(target_date, target_time):
                    await asyncio.sleep(0)
                    continue
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

        dev_mode = parse_bool_flag(reservation_data.get("devMode", False))
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

    async def _rewarm_if_needed(
        self, target_date: str, date_exists: bool, until_open: float | None = None
    ) -> None:
        """Reload the parked tab when it would otherwise be useless or stale."""
        if self._page is None:
            return
        # Never reload across the opening boundary. The reload takes a couple of
        # seconds during which nothing else runs, it cannot render a date the
        # server has not opened yet, and one landing here would push the strike's
        # own reload past the moment that decides the run.
        if until_open is not None and 0 < until_open <= self.OPEN_BLACKOUT_SECONDS:
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

    def _seconds_until_open(self) -> float | None:
        """Server-clock seconds until the published opening moment.

        None when the item publishes no opening time, which is the signal for
        every caller to behave exactly as it did before this existed.
        """
        if self._open_at_epoch is None or self.clock is None:
            return None
        return self.clock.seconds_until(self._open_at_epoch)

    async def _refresh_open_schedule(self, target_date: str) -> bool | None:
        """Re-resolve rolling opening metadata instead of freezing startup data."""
        if self.api is None or self.clock is None:
            return None
        self._last_open_schedule_refresh = time.monotonic()
        try:
            meta = await asyncio.to_thread(self.api.fetch_item_meta)
            resolved = await asyncio.to_thread(
                self.api.resolve_target_open_at, target_date, meta
            )
        except (NaverApiError, TypeError, ValueError) as exc:
            self._log_throttled(
                "open_schedule_refresh",
                f"[경고] 네이버 오픈 일정 재확인 실패: {exc}",
                "warning", 30.0,
            )
            return None

        self._uses_open_schedule = bool(meta.uses_open_schedule)
        new_epoch = (
            resolved.timestamp()
            if meta.uses_open_schedule and resolved is not None else None
        )
        old_epoch = self._open_at_epoch
        if new_epoch is None:
            self._open_at_epoch = None
            self._open_strike_pending = False
            return old_epoch is not None
        if old_epoch is not None and abs(new_epoch - old_epoch) < 0.5:
            return False

        self._open_at_epoch = new_epoch
        remaining = self.clock.seconds_until(new_epoch)
        self._open_strike_pending = remaining > 0
        self.log(
            f"[정보] 네이버 오픈 일정을 최신 공개 시간표 기준으로 갱신했습니다 · "
            f"{resolved:%Y-%m-%d %H:%M:%S} · "
            f"{'오픈까지 ' + self._format_remaining(remaining) if remaining > 0 else '이미 오픈 시각 경과'}",
            "info",
        )
        return True

    def _claim_open_strike(self, until_open: float | None) -> bool:
        """Claim the one-shot opening turn once the boundary is within reach."""
        if not self._open_strike_pending or self._page is None:
            return False
        if until_open is None or until_open > self.OPEN_ARM_SECONDS:
            return False
        # Claim before yielding so a slow reload cannot let a second turn in.
        self._open_strike_pending = False
        return True

    async def _wait_for_open(self) -> None:
        """Hold until the server clock reaches the opening moment.

        Coarse sleeps while there is time, then a tight spin for the last half
        second. Being a few hundred milliseconds late is the whole failure mode
        this exists to remove, and the spin costs a handful of wake-ups.
        """
        while not self.stop_event.is_set():
            remaining = self._seconds_until_open()
            if remaining is None or remaining <= 0:
                return
            await asyncio.sleep(0.05 if remaining > 0.5 else 0.005)

    async def _sync_clock_before_open(self) -> bool:
        """Refresh the server clock once in the final safe pre-open window."""
        if self._final_clock_sync_attempted or self.clock is None:
            return False
        self._final_clock_sync_attempted = True
        precise_sync = getattr(self.clock, "sync_precise", None)
        if not callable(precise_sync):
            return False
        synced = await asyncio.to_thread(
            precise_sync,
            self.API_PREOPEN_CLOCK_SAMPLES,
            False,
        )
        if not synced:
            return False
        precision_ms = float(self.clock.last_precision or 0.0) * 1000
        self.log(
            f"[정보] 오픈 직전 서버 시계 정밀 보정 완료 · "
            f"최저 지연 표본 오차 약 {precision_ms:.0f}ms",
            "success",
        )
        return True

    def _api_one_way_seconds(self) -> float:
        browser_samples = []
        if self._api_submitter is not None:
            browser_samples.extend(
                getattr(self._api_submitter, "safe_rtt_samples", ()) or ()
            )
            browser_samples.append(getattr(self._api_submitter, "last_rtt", None))

        def normalized(values):
            result = []
            for value in values:
                try:
                    candidate = float(value)
                except (TypeError, ValueError):
                    continue
                if 0.005 <= candidate <= 3.0:
                    result.append(candidate)
            return result

        transport_samples = normalized(browser_samples)
        if not transport_samples and self.api is not None:
            transport_samples = normalized((getattr(self.api, "last_rtt", None),))
        if transport_samples:
            # The quickest warmed same-origin read best represents network
            # transit. Higher percentiles and mutation response RTT include
            # server queue/processing time and caused excessive early sends.
            transport_rtt = min(transport_samples)
        else:
            transport_rtt = 0.10
        precision = 0.0
        if self.clock is not None:
            try:
                precision = max(
                    0.0,
                    min(0.10, float(getattr(self.clock, "last_precision", 0.0) or 0.0)),
                )
            except (TypeError, ValueError):
                precision = 0.0
        one_way = transport_rtt / 2
        target_before_open = float(
            getattr(self._timing_profile, "target_before_open_seconds", 0.0)
            or DEFAULT_TARGET_BEFORE_OPEN_SECONDS
        )
        lead = min(
            self.API_SEND_MAX_LEAD_SECONDS,
            max(
                self.API_SEND_MIN_LEAD_SECONDS,
                one_way + target_before_open,
            ),
        )
        self._last_api_lead_detail = (
            f"최저 RTT {transport_rtt * 1000:.0f}ms · "
            f"서버 선점 도착 목표 -{target_before_open * 1000:.0f}ms · "
            f"시계 오차 {precision * 1000:.0f}ms"
        )
        return lead

    async def _wait_for_api_send(self) -> None:
        """Start the fetch early enough for it to reach Naver near the boundary."""
        lead = self._api_one_way_seconds()
        while not self.stop_event.is_set():
            remaining = self._seconds_until_open()
            if remaining is None or remaining <= lead:
                return
            await asyncio.sleep(0.05 if remaining - lead > 0.5 else 0.005)

    def _disable_api_submit(self, reason: str) -> None:
        self._api_submit_enabled = False
        self._api_submit_blocked = True
        self.log(
            f"[경고] API 직접 제출 비활성화 · {reason[:120]} · "
            "브라우저 제출로 전환합니다.",
            "warning",
        )

    def _record_submit_rtt(self, elapsed_ms: float | None) -> None:
        if elapsed_ms is None:
            return
        try:
            seconds = float(elapsed_ms) / 1000
        except (TypeError, ValueError):
            return
        if 0.005 <= seconds <= 3.0:
            self._recent_submit_rtt.append(seconds)
            del self._recent_submit_rtt[:-5]

    def _timing_observation_payload(self) -> dict[str, Any]:
        timing = (
            getattr(self._api_submitter, "last_armed_timing", {})
            if self._api_submitter is not None
            else {}
        ) or {}
        diagnostics = (
            getattr(self._api_submitter, "last_armed_diagnostics", {})
            if self._api_submitter is not None
            else {}
        ) or {}
        server_open_at = timing.get("serverOpenAt")
        started_at = timing.get("startedAt")
        last_started_at = timing.get("lastStartedAt")
        outbound_ms = timing.get("estimatedOutboundMs")
        dispatch_offset_ms = None
        arrival_offset_ms = None
        last_dispatch_offset_ms = None
        last_arrival_offset_ms = None
        if (
            isinstance(server_open_at, (int, float))
            and server_open_at > 0
            and isinstance(started_at, (int, float))
        ):
            dispatch_offset_ms = float(started_at) - float(server_open_at)
            if isinstance(outbound_ms, (int, float)):
                arrival_offset_ms = dispatch_offset_ms + float(outbound_ms)
        if (
            isinstance(server_open_at, (int, float))
            and server_open_at > 0
            and isinstance(last_started_at, (int, float))
            and last_started_at > 0
        ):
            last_dispatch_offset_ms = float(last_started_at) - float(server_open_at)
            if isinstance(outbound_ms, (int, float)):
                last_arrival_offset_ms = (
                    last_dispatch_offset_ms + float(outbound_ms)
                )
        payload = {
            "dispatch_offset_ms": dispatch_offset_ms,
            "estimated_arrival_offset_ms": arrival_offset_ms,
            "last_dispatch_offset_ms": last_dispatch_offset_ms,
            "last_estimated_arrival_offset_ms": last_arrival_offset_ms,
            "transport_rtt_ms": (
                float(outbound_ms) * 2
                if isinstance(outbound_ms, (int, float))
                else None
            ),
            "submit_rtt_ms": (
                float(self._recent_submit_rtt[-1]) * 1000
                if self._recent_submit_rtt
                else None
            ),
            "clock_rtt_ms": timing.get("clockRttMs"),
            "clock_uncertainty_ms": timing.get("clockUncertaintyMs"),
            "clock_spread_ms": timing.get("clockSpreadMs"),
            "ttfb_ms": diagnostics.get("ttfbMs"),
            "response_ms": diagnostics.get("responseMs"),
            "attempts": timing.get("attempts"),
            "not_open_attempts": timing.get("notOpenAttempts"),
            "http_status": diagnostics.get("httpStatus"),
        }
        return {key: value for key, value in payload.items() if value is not None}

    def _record_api_timing_result(
        self,
        *,
        outcome: str,
        response_code: str = "",
        booking_confirmed: bool = False,
    ) -> None:
        if not self._timing_profile.key:
            return
        remaining = None
        for sample in reversed(self._last_post_submit_inventory):
            candidate = sample.get("remaining") if isinstance(sample, dict) else None
            if isinstance(candidate, (int, float)):
                remaining = int(candidate)
                break
        timing_payload = self._timing_observation_payload()
        try:
            not_open_attempts = int(
                timing_payload.get("not_open_attempts", 0) or 0
            )
        except (TypeError, ValueError):
            not_open_attempts = 0
        learned_outcome = outcome
        if not_open_attempts > 0:
            learned_outcome = (
                "success_after_notopen"
                if booking_confirmed
                else "refused_after_notopen"
            )
        business_id, biz_item_id, payment_mode = self._timing_profile.key.split("|", 2)
        try:
            update = record_timing_observation(
                business_id,
                biz_item_id,
                payment_mode,
                outcome=learned_outcome,
                response_code=response_code,
                booking_confirmed=booking_confirmed,
                inventory_remaining=remaining,
                timing=timing_payload,
            )
        except Exception as exc:
            self._log_throttled(
                "timing_history_write",
                f"[정보] 네이버 선점 타이밍 기록을 저장하지 못했습니다 "
                f"({type(exc).__name__}).",
                "info",
                30.0,
            )
            return
        self._timing_profile = update.profile
        if abs(update.adjustment_seconds) >= 0.0005:
            direction = "앞당김" if update.adjustment_seconds > 0 else "늦춤"
            self.log(
                f"[정보] 상품별 선점 타이밍 학습 갱신 · 다음 서버 도착 목표 "
                f"-{update.profile.target_before_open_seconds * 1000:.0f}ms · "
                f"{abs(update.adjustment_seconds) * 1000:.0f}ms {direction}",
                "success",
            )

    async def _observe_post_submit_inventory(
        self,
        reservation_data: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Capture read-only inventory evidence after one ambiguous mutation."""
        if self.api is None:
            return []
        data = reservation_data or {}
        target_date = str(data.get("reservationDate") or "")
        target_time = str(data.get("reservationTime") or "")[:5]
        if not target_date or not target_time:
            return []
        started = time.monotonic()
        samples: list[dict[str, Any]] = []
        for offset in self.API_POST_SUBMIT_INVENTORY_OFFSETS:
            wait_for = started + float(offset) - time.monotonic()
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            try:
                slot = await asyncio.wait_for(
                    asyncio.to_thread(self.api.find_slot, target_date, target_time),
                    timeout=1.0,
                )
            except (asyncio.TimeoutError, NaverApiError, OSError):
                samples.append({
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "status": "조회 실패",
                })
                continue
            if slot is None:
                samples.append({
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "status": "미공개",
                })
                continue
            reason = slot.blocked_reason(self.clock.now_kst() if self.clock else None)
            samples.append({
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "slot_id": slot.slot_id,
                "stock": slot.stock,
                "remaining": slot.remaining,
                "status": reason or "예약 가능",
            })
        self._last_post_submit_inventory = samples
        readable = []
        for sample in samples:
            elapsed = sample.get("elapsed_ms", 0)
            if "remaining" in sample:
                readable.append(
                    f"+{float(elapsed):.0f}ms {sample['remaining']}/{sample.get('stock', '?')}"
                )
            else:
                readable.append(f"+{float(elapsed):.0f}ms {sample.get('status', '알 수 없음')}")
        if readable:
            self.log(
                "[정보] 제출 후 재고 관측(읽기 전용) · " + " · ".join(readable),
                "info",
            )
        return samples

    async def _refused_slot_changed(
        self, target_date: str, target_time: str
    ) -> bool:
        """Recheck RT47 with one read before honoring the longer backoff.

        This never sends another reservation mutation.  It only removes the
        refusal guard when Naver's fresh public inventory has changed to a new,
        still-bookable state; the normal loop then owns any later submission.
        """
        refused_signature = self._api_refused_signature
        if self.api is None or refused_signature is None:
            return False
        try:
            slot = await asyncio.wait_for(
                asyncio.to_thread(self.api.find_slot, target_date, target_time),
                timeout=self.API_REFUSED_RECHECK_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, NaverApiError, OSError):
            return False
        if slot is None:
            return False
        reason = slot.blocked_reason(self.clock.now_kst() if self.clock else None)
        fresh_signature = self._slot_signature(slot, reason)
        if reason is not None or fresh_signature == refused_signature:
            return False
        self._api_refused_signature = None
        self.log(
            f"[정보] 서버 거절 직후 최신 재고 변화 확인 · "
            f"slotId={slot.slot_id} 잔여 {slot.remaining}/{slot.stock} · "
            "긴 대기 없이 감시를 재개합니다.",
            "success",
        )
        return True

    async def _reconcile_ambiguous_api_submit(
        self,
        *,
        reservation_data: dict[str, Any] | None,
        dev_mode: bool,
    ) -> tuple[str, str] | None:
        """Confirm this account's booking without issuing another mutation."""
        if self._api_submitter is None or self._api_preparation is None:
            return None
        reconcile = getattr(self._api_submitter, "reconcile_upcoming_booking", None)
        if not callable(reconcile):
            return None
        data = reservation_data or {}
        payload = self._api_preparation.payload
        target_date = str(data.get("reservationDate") or "")
        target_time = str(data.get("reservationTime") or "")[:5]
        business_id = str(payload.get("businessId") or "")
        item_name = str(
            (self._api_biz_item or {}).get("name")
            or data.get("themeLabel")
            or ""
        )
        if not target_date or not target_time:
            return None
        self._last_post_submit_inventory = []
        inventory_task = asyncio.create_task(
            self._observe_post_submit_inventory(reservation_data)
        )
        self.log(
            "[정보] 제출 응답이 불명확해 추가 POST 없이 본인 네이버 예약내역을 확인합니다.",
            "warning",
        )
        try:
            evidence = await reconcile(
                target_date=target_date,
                target_time=target_time,
                business_id=business_id,
                item_name=item_name,
                attempts=self.API_RECONCILE_ATTEMPTS,
                window_seconds=self.API_RECONCILE_WINDOW_SECONDS,
            )
        except Exception:
            await inventory_task
            return None
        if not getattr(evidence, "found", False) or not getattr(
            evidence, "booking_id", ""
        ):
            await inventory_task
            return None

        if not inventory_task.done():
            inventory_task.cancel()
            try:
                await inventory_task
            except asyncio.CancelledError:
                pass

        booking_id = str(evidence.booking_id)
        landing_url = str(getattr(evidence, "url", "") or "")
        if landing_url.startswith("/"):
            landing_url = urllib.parse.urljoin(
                "https://m.booking.naver.com", landing_url
            )
        self._api_submit_state = "success"
        self.log(
            f"[정보] 본인 예약내역 대조 성공 · 예약번호 {booking_id} · "
            "동일 상품·날짜·시간 확인",
            "success",
        )
        if self._api_preparation.requires_checkout:
            return await self._continue_npay_checkout(
                booking_id=booking_id,
                payment_url=landing_url,
                dev_mode=dev_mode,
                navigate_immediately=True,
            )
        return "success", f"예약번호 {booking_id} · 본인 예약내역에서 확정"

    async def _handle_api_submit_result(
        self,
        result,
        *,
        signature: tuple[Any, ...] | None,
        reservation_data: dict[str, Any] | None,
        dev_mode: bool,
    ) -> tuple[str, str]:
        """Handle one known result without issuing any additional mutation."""
        if result.outcome == SubmitOutcome.SUCCESS:
            self._api_submit_state = "success"
            self._api_delayed_open_started = 0.0
            self._api_delayed_open_slow_logged = False
            self._record_api_timing_result(
                outcome=SubmitOutcome.SUCCESS,
                response_code=result.code,
                booking_confirmed=True,
            )
            if self._api_preparation is not None and self._api_preparation.requires_checkout:
                return await self._continue_npay_checkout(
                    booking_id=result.booking_id,
                    payment_url=self._normalize_payment_url(result.url),
                    dev_mode=dev_mode,
                    navigate_immediately=True,
                )
            return (
                "success",
                f"예약번호 {result.booking_id}"
                + (f" · {result.url}" if result.url else ""),
            )
        if result.outcome == SubmitOutcome.NOT_OPEN:
            self._api_submit_state = "idle"
            return "notopen", result.detail
        if result.outcome in {
            SubmitOutcome.REFUSED,
            SubmitOutcome.DUPLICATED,
            SubmitOutcome.UNKNOWN,
            SubmitOutcome.ERROR,
        }:
            # RT47, duplicate, timeout and malformed-response paths can all be
            # partial/ambiguous after the mutation crossed the network.  Freeze
            # final submission and reconcile the authenticated account instead
            # of risking a second reservation.
            self._api_submit_state = "uncertain"
            self._api_delayed_open_started = 0.0
            self._api_delayed_open_slow_logged = False
            self._api_submit_enabled = False
            self._api_submit_blocked = True
            self._api_refused_signature = (
                signature if signature is not None else getattr(self, "_last_signature", None)
            )
            recovered = await self._reconcile_ambiguous_api_submit(
                reservation_data=reservation_data,
                dev_mode=dev_mode,
            )
            if recovered is not None:
                self._record_api_timing_result(
                    outcome=SubmitOutcome.SUCCESS,
                    response_code=result.code,
                    booking_confirmed=True,
                )
                return recovered
            self._record_api_timing_result(
                outcome=result.outcome,
                response_code=result.code,
                booking_confirmed=False,
            )
            suffix = " · 본인 예약내역에서 아직 확정되지 않음 · 추가 POST 없음"
            if result.outcome == SubmitOutcome.REFUSED:
                return "unknown", result.detail + suffix
            if result.outcome == SubmitOutcome.DUPLICATED:
                return "duplicate", result.detail + suffix
            return "unknown", result.detail + suffix
        self._api_submit_state = "idle"
        self._api_delayed_open_started = 0.0
        self._api_delayed_open_slow_logged = False
        self._disable_api_submit(result.detail)
        return "fallback", result.detail

    async def _submit_api_armed(
        self,
        signature: tuple[Any, ...] | None = None,
        *,
        reservation_data: dict[str, Any] | None = None,
        dev_mode: bool = False,
    ) -> tuple[str, str]:
        """Arm one browser-internal mutation before the published opening time."""
        if self._api_submit_state != "idle":
            return (
                "unknown",
                f"API 제출 상태 {self._api_submit_state} · 추가 POST를 보내지 않습니다",
            )
        if (
            not self._api_submit_enabled
            or self._api_submitter is None
            or self._api_preparation is None
            or not self._api_preparation.ready
        ):
            return "fallback", "API 직접 제출이 준비되지 않았습니다"
        if not hasattr(self._api_submitter, "arm_submit_at"):
            return await self._submit_api_first(
                signature,
                reservation_data=reservation_data,
                dev_mode=dev_mode,
            )
        remaining = self._seconds_until_open()
        if remaining is None or remaining < self.API_BROWSER_ARM_MIN_SECONDS:
            return await self._submit_api_first(
                signature,
                reservation_data=reservation_data,
                dev_mode=dev_mode,
            )
        lead = self._api_one_way_seconds()
        target_before_open = float(
            getattr(self._timing_profile, "target_before_open_seconds", 0.0)
            or DEFAULT_TARGET_BEFORE_OPEN_SECONDS
        )
        delay = max(0.0, remaining - lead)
        try:
            server_time_arm = getattr(
                self._api_submitter, "arm_submit_at_server_time", None
            )
            if callable(server_time_arm) and self._open_at_epoch is not None:
                arm_id = await server_time_arm(
                    self._api_preparation.payload,
                    delay,
                    open_at_epoch=self._open_at_epoch,
                    lead_seconds=lead,
                    retry_lead_seconds=max(
                        0.0,
                        lead - target_before_open,
                    ),
                    target_arrival_before_open_seconds=target_before_open,
                )
            else:
                arm_id = await self._api_submitter.arm_submit_at(
                    self._api_preparation.payload, delay
                )
        except Exception as exc:
            self.log(
                f"[정보] 브라우저 내부 예약 타이머 준비 실패 ({type(exc).__name__}) · "
                "기존 직접 제출로 진행합니다.",
                "info",
            )
            return await self._submit_api_first(
                signature,
                reservation_data=reservation_data,
                dev_mode=dev_mode,
            )
        self._api_submit_state = "inflight"
        armed_timing = getattr(self._api_submitter, "last_armed_timing", {}) or {}
        clock_samples = armed_timing.get("clockSampleCount")
        clock_rtt = armed_timing.get("clockRttMs")
        clock_detail = ""
        if (
            isinstance(clock_samples, (int, float))
            and clock_samples > 0
            and isinstance(clock_rtt, (int, float))
        ):
            clock_detail = (
                f" · Chrome 동일 연결 서버 시계 {int(clock_samples)}회 "
                f"(최저 RTT {float(clock_rtt):.0f}ms)"
            )
        applied_lead_ms = armed_timing.get("appliedLeadMs")
        logged_lead = (
            float(applied_lead_ms) / 1000
            if isinstance(applied_lead_ms, (int, float)) and applied_lead_ms > 0
            else lead
        )
        uncertainty_ms = armed_timing.get("clockUncertaintyMs")
        if isinstance(uncertainty_ms, (int, float)) and uncertainty_ms > 0:
            clock_detail += f" · 경계 추정 오차 ±{float(uncertainty_ms):.0f}ms 이내"
        self.log(
            f"[정보] 브라우저 내부 API 제출 예약 · 추정 오픈 대비 {-logged_lead:+.3f}초 · "
            f"{self._last_api_lead_detail}{clock_detail} · "
            "단일 선점 흐름으로 대기합니다.",
            "warning",
        )
        browser_delay_ms = armed_timing.get("delayMs")
        if isinstance(browser_delay_ms, (int, float)):
            delay = max(0.0, float(browser_delay_ms) / 1000)
        due = time.monotonic() + delay
        while (
            not self.stop_event.is_set()
            and time.monotonic() < due - self.API_BROWSER_ARM_FINAL_QUIET_SECONDS
        ):
            wait_for = due - time.monotonic() - self.API_BROWSER_ARM_FINAL_QUIET_SECONDS
            await asyncio.sleep(min(0.10, max(0.01, wait_for)))
        if self.stop_event.is_set():
            await self._api_submitter.cancel_armed_submit(arm_id)
            self._api_submit_state = "idle"
            return "stopped", "중지됨"
        # Keep CDP traffic out of the final timer window; Chrome owns the exact
        # dispatch and Python reads the result only afterwards.
        await asyncio.sleep(max(0.0, due - time.monotonic() + self.API_BROWSER_ARM_FINAL_QUIET_SECONDS))
        while not self.stop_event.is_set():
            state, result, elapsed_ms = await self._api_submitter.read_armed_submit(
                arm_id, self._api_preparation.payload
            )
            if result is not None:
                self._record_submit_rtt(elapsed_ms)
                timing = getattr(self._api_submitter, "last_armed_timing", {}) or {}
                due_at = timing.get("dueAt")
                started_at = timing.get("startedAt")
                server_open_at = timing.get("serverOpenAt")
                dispatch_detail = ""
                if isinstance(due_at, (int, float)) and isinstance(started_at, (int, float)):
                    lateness_ms = float(started_at) - float(due_at)
                    dispatch_detail = f" · 타이머 발사 오차 {lateness_ms:+.1f}ms"
                if (
                    isinstance(server_open_at, (int, float))
                    and server_open_at > 0
                    and isinstance(started_at, (int, float))
                ):
                    open_offset_ms = float(started_at) - float(server_open_at)
                    dispatch_detail += (
                        f" · 추정 네이버 오픈 기준 발사 {open_offset_ms:+.1f}ms"
                    )
                attempts = timing.get("attempts")
                attempt_detail = (
                    f" · POST {int(attempts)}회"
                    if isinstance(attempts, (int, float)) and attempts > 0
                    else " · POST 횟수 확인 불가"
                )
                outbound_ms = timing.get("estimatedOutboundMs")
                if (
                    isinstance(outbound_ms, (int, float))
                    and isinstance(server_open_at, (int, float))
                    and server_open_at > 0
                    and isinstance(started_at, (int, float))
                ):
                    arrival_offset_ms = (
                        float(started_at) + float(outbound_ms) - float(server_open_at)
                    )
                    dispatch_detail += f" · 추정 서버 도착 {arrival_offset_ms:+.1f}ms"
                last_started_at = timing.get("lastStartedAt")
                if (
                    isinstance(attempts, (int, float))
                    and attempts > 1
                    and isinstance(server_open_at, (int, float))
                    and server_open_at > 0
                    and isinstance(last_started_at, (int, float))
                    and last_started_at > 0
                ):
                    retry_dispatch_ms = (
                        float(last_started_at) - float(server_open_at)
                    )
                    dispatch_detail += (
                        f" · 마지막 재시도 발사 {retry_dispatch_ms:+.1f}ms"
                    )
                    if isinstance(outbound_ms, (int, float)):
                        retry_arrival_ms = retry_dispatch_ms + float(outbound_ms)
                        dispatch_detail += (
                            f"/도착 {retry_arrival_ms:+.1f}ms"
                        )
                diagnostics = getattr(
                    self._api_submitter, "last_armed_diagnostics", {}
                ) or {}
                http_status = diagnostics.get("httpStatus")
                ttfb_ms = diagnostics.get("ttfbMs")
                response_ms = diagnostics.get("responseMs")
                network_detail = ""
                if isinstance(http_status, (int, float)) and http_status > 0:
                    network_detail += f" · HTTP {int(http_status)}"
                if isinstance(ttfb_ms, (int, float)) and ttfb_ms > 0:
                    network_detail += f" · TTFB {float(ttfb_ms):.0f}ms"
                if isinstance(response_ms, (int, float)) and response_ms > 0:
                    network_detail += f" · 본문 {float(response_ms):.0f}ms"
                if elapsed_ms is not None:
                    self.log(
                        f"[정보] 브라우저 내부 API 제출 응답 · {result.outcome} · "
                        f"RTT {elapsed_ms:.0f}ms{dispatch_detail}{attempt_detail}"
                        f"{network_detail} · 코드 {result.code or '-'}",
                        "success" if result.outcome == SubmitOutcome.SUCCESS else "info",
                    )
                outcome, detail = await self._handle_api_submit_result(
                    result,
                    signature=signature,
                    reservation_data=reservation_data,
                    dev_mode=dev_mode,
                )
                if outcome == "notopen":
                    # Existing policy: only an explicit not-open response gets a
                    # bounded retry; no parallel or duplicate submission occurs.
                    return await self._submit_api_first(
                        signature,
                        reservation_data=reservation_data,
                        dev_mode=dev_mode,
                    )
                return outcome, detail
            if state == "cancelled":
                self._api_submit_state = "idle"
                return "stopped", "중지됨"
            await asyncio.sleep(self.API_BROWSER_ARM_STATUS_SECONDS)
        await self._api_submitter.cancel_armed_submit(arm_id)
        self._api_submit_state = "idle"
        return "stopped", "중지됨"

    async def _submit_api_first(
        self,
        signature: tuple[Any, ...] | None = None,
        *,
        reservation_data: dict[str, Any] | None = None,
        dev_mode: bool = False,
    ) -> tuple[str, str]:
        """Send the prepared mutation, retrying only the server's not-open reply."""
        if self._api_submit_state != "idle":
            return (
                "unknown",
                f"API 제출 상태 {self._api_submit_state} · 추가 POST를 보내지 않습니다",
            )
        if (
            not self._api_submit_enabled
            or self._api_submitter is None
            or self._api_preparation is None
            or not self._api_preparation.ready
        ):
            return "fallback", "API 직접 제출이 준비되지 않았습니다"

        deadline = time.monotonic() + self.API_NOT_OPEN_WINDOW_SECONDS
        for attempt in range(1, self.API_SUBMIT_MAX_ATTEMPTS + 1):
            self._api_submit_state = "inflight"
            sent_at = time.monotonic()
            offset = self._seconds_until_open()
            offset_text = (
                f"{-offset:+.3f}초"
                if offset is not None
                else "오픈 시각 정보 없음"
            )
            self.log(
                f"[정보] API 직접 제출 전송 · {attempt}/"
                f"{self.API_SUBMIT_MAX_ATTEMPTS} · 오픈 대비 {offset_text}",
                "warning",
            )
            result = await self._api_submitter.submit(
                self._api_preparation.payload
            )
            elapsed_ms = (time.monotonic() - sent_at) * 1000
            self._record_submit_rtt(elapsed_ms)
            self.log(
                f"[정보] API 직접 제출 응답 · {result.outcome} · "
                f"RTT {elapsed_ms:.0f}ms",
                "success" if result.outcome == SubmitOutcome.SUCCESS else "info",
            )

            outcome, detail = await self._handle_api_submit_result(
                result,
                signature=signature,
                reservation_data=reservation_data,
                dev_mode=dev_mode,
            )
            if outcome == "notopen":
                if (
                    attempt < self.API_SUBMIT_MAX_ATTEMPTS
                    and time.monotonic() < deadline
                ):
                    await asyncio.sleep(self.API_NOT_OPEN_RETRY_SECONDS)
                    continue
                self._last_post_submit_inventory = []
                self._record_api_timing_result(
                    outcome=SubmitOutcome.NOT_OPEN,
                    response_code=result.code,
                    booking_confirmed=False,
                )
                if self._api_delayed_open_started <= 0:
                    self._api_delayed_open_started = time.monotonic()
                    self._api_delayed_open_slow_logged = False
                return "delayed_open", detail
            return outcome, detail

        return "fallback", "API 직접 제출 제한 시간 초과"

    async def _wait_for_delayed_api_open(
        self,
        target_date: str,
        target_time: str,
        reservation_data: dict[str, Any],
        dev_mode: bool,
    ) -> tuple[str, str]:
        """Wait for a real page transition after an explicit NOT_OPEN reply.

        The slot API can advertise inventory before the booking gate accepts a
        mutation.  Page/timetable reads are safe evidence of that later
        transition; RT47 and other ambiguous replies never enter this path.
        """
        if self._api_delayed_open_started <= 0:
            self._api_delayed_open_started = time.monotonic()
        self.log(
            "[정보] 네이버가 아직 미오픈으로 확인했습니다 · 추가 제출 없이 "
            "실제 예약 화면 오픈을 최대 5분 집중 감시합니다.",
            "warning",
        )

        while not self.stop_event.is_set():
            elapsed = time.monotonic() - self._api_delayed_open_started
            intensive = elapsed < self.API_DELAYED_OPEN_ACTIVE_WINDOW_SECONDS
            if not intensive and not self._api_delayed_open_slow_logged:
                self._api_delayed_open_slow_logged = True
                self.log(
                    "[정보] 지연 오픈 집중 감시 5분 경과 · 예약을 포기하지 않고 "
                    "5초 간격의 읽기 전용 확인을 계속합니다.",
                    "info",
                )

            try:
                rendered = await self._goto_item(
                    target_date,
                    timeout_ms=self.API_DELAYED_OPEN_PAGE_TIMEOUT_MS,
                )
            except Exception as exc:
                rendered = False
                self._log_throttled(
                    "delayed_open_page_error",
                    f"[정보] 지연 오픈 화면 확인이 늦어지고 있습니다 "
                    f"({type(exc).__name__}) · 계속 확인합니다.",
                    "info",
                    15.0,
                )

            self._last_warm = time.monotonic()
            self._warmed_for_date = bool(rendered)
            if rendered:
                self.log(
                    f"[정보] 실제 예약 화면 오픈 확인 · {target_date} {target_time} · "
                    "최신 세션과 슬롯으로 단일 선점 흐름을 재개합니다.",
                    "success",
                )
                await self._refresh_api_submit(reservation_data)
                outcome, detail = await self._submit_api_first(
                    getattr(self, "_last_signature", None),
                    reservation_data=reservation_data,
                    dev_mode=dev_mode,
                )
                if outcome != "delayed_open":
                    return outcome, detail

            delay = (
                self.API_DELAYED_OPEN_ACTIVE_POLL_SECONDS
                if intensive
                else self.API_DELAYED_OPEN_SLOW_POLL_SECONDS
            )
            self._log_throttled(
                "delayed_open_wait",
                f"[정보] 설정 시각 이후 실제 예약 화면 대기 중 · "
                f"{elapsed:.0f}초 경과 · 읽기 전용 확인을 계속합니다.",
                "info",
                10.0,
            )
            await asyncio.sleep(delay)

        return "stopped", "중지됨"

    async def _strike_at_open(
        self, target_date: str, target_time: str, reservation_data, dev_mode: bool
    ) -> tuple[str, str]:
        """Own the opening moment: direct mutation first, browser fallback second."""
        if self._api_submit_state in {"inflight", "success", "uncertain"}:
            return (
                "unknown",
                f"API 제출 상태 {self._api_submit_state} · 추가 POST를 보내지 않습니다",
            )
        remaining = self._seconds_until_open()
        if self._api_prepare_pending:
            if remaining is not None and remaining > 0:
                self.log(
                    f"[정보] 오픈 {remaining * 1000:.0f}ms 전 · "
                    "슬롯 상세 공개 시각까지 대기합니다.",
                    "warning",
                )
                await self._wait_for_open()
            if self.stop_event.is_set():
                return "error", "중지됨"
            await self._prepare_api_submit(reservation_data, dev_mode=False)
            remaining = self._seconds_until_open()

        if self._api_may_submit(dev_mode):
            if (
                remaining is not None
                and remaining >= self.API_PREFLIGHT_MIN_SECONDS
            ):
                await self._refresh_api_submit(reservation_data)
                remaining = self._seconds_until_open()
            if remaining is not None and remaining > 0:
                self.log(
                    f"[정보] 오픈 {remaining * 1000:.0f}ms 전 · "
                    "브라우저 내부 API 제출 시각을 준비합니다.",
                    "warning",
                )
            if self.stop_event.is_set():
                return "error", "중지됨"

            outcome, detail = await self._submit_api_armed(
                reservation_data=reservation_data,
                dev_mode=dev_mode,
            )
            if outcome == "delayed_open":
                return await self._wait_for_delayed_api_open(
                    target_date,
                    target_time,
                    reservation_data,
                    dev_mode,
                )
            if outcome != "fallback":
                return outcome, detail
            remaining = self._seconds_until_open()

        if remaining is not None and remaining > 0:
            self.log(
                f"[정보] 오픈 {remaining * 1000:.0f}ms 전 · 서버 시각에 맞춰 대기합니다.",
                "warning",
            )
            await self._wait_for_open()
        if self.stop_event.is_set():
            return "error", "중지됨"

        self.log(
            f"[정보] 네이버 서버 오픈 시각 도달 · {target_date} 예약 화면을 "
            "새로고침하고 즉시 제출합니다.",
            "warning",
        )
        started = time.monotonic()
        rendered = False
        attempt = 0
        for attempt in range(1, self.OPEN_RELOAD_ATTEMPTS + 1):
            if self.stop_event.is_set():
                return "error", "중지됨"
            try:
                rendered = await self._goto_item(
                    target_date, timeout_ms=self.OPEN_RELOAD_TIMEOUT_MS
                )
            except Exception as exc:
                rendered = False
                self.log(
                    f"[경고] 오픈 시각 새로고침 실패 ({type(exc).__name__}) · "
                    f"{attempt}/{self.OPEN_RELOAD_ATTEMPTS}회",
                    "warning",
                )
            self._last_warm = time.monotonic()
            self._warmed_for_date = rendered
            if rendered:
                break

        elapsed = (time.monotonic() - started) * 1000
        since_open = self._seconds_until_open()
        offset = f"+{-since_open:.2f}초" if since_open is not None else "-"
        self.log(
            f"[정보] 오픈 {offset} · 시간표 "
            f"{'렌더링 완료' if rendered else '표시되지 않음'} "
            f"(새로고침 {attempt}회 {elapsed:.0f}ms)",
            "success" if rendered else "warning",
        )
        if not rendered:
            return "notready", (
                f"{target_date} 시간표가 오픈 직후에도 렌더링되지 않았습니다 "
                f"(li.time_item 0개)"
            )
        return await self._submit(target_date, target_time, reservation_data, dev_mode)

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

    def _adopt_browser_account(
        self,
        account: NaverAccount,
        *,
        invalidate_preparation: bool = True,
    ) -> None:
        """Make the active Chrome account authoritative for future submits."""
        previous = self._api_account
        previous_identity = (
            previous.user_id or previous.csrf_token
            if previous is not None and previous.is_logged_in
            else ""
        )
        current_identity = account.user_id or account.csrf_token
        changed = bool(
            previous is not None
            and previous.is_logged_in
            and account.is_logged_in
            and (
                previous_identity != current_identity
                or previous.csrf_token != account.csrf_token
            )
        )
        self._api_account = account
        if not changed:
            return

        self._api_submit_blocked = False
        self._api_refused_signature = None
        self._npay_booking_id = ""
        if invalidate_preparation:
            self._api_submit_enabled = False
            self._api_preparation = None
            self._api_prepare_pending = True

        account_changed = bool(
            previous is not None
            and previous.user_id
            and account.user_id
            and previous.user_id != account.user_id
        )
        self.log(
            "[정보] 현재 Chrome의 네이버 계정 변경을 반영했습니다. "
            "이전 제출 준비값을 새 로그인 기준으로 갱신합니다."
            if account_changed
            else "[정보] 네이버 로그인 세션 갱신을 반영했습니다.",
            "success",
        )

    async def _live_browser_account(self) -> NaverAccount | None:
        if self._page is None:
            return None
        try:
            url = self._page.url or ""
        except Exception:
            return None
        if "booking.naver.com" not in url:
            return None

        submitter = NaverBrowserSubmitter(self._page)
        account = await submitter.fetch_account()
        if not submitter.last_account_fetch_ok:
            return None
        return account

    async def _login_state(self) -> bool | None:
        # The Apollo cache is only a page-load snapshot. After logout/login in
        # the same Chrome window it can still describe the old account forever.
        # Naver's live account query uses the current browser cookie jar and is
        # therefore authoritative whenever it answers successfully.
        account = await self._live_browser_account()
        if account is not None:
            if account.is_logged_in:
                self._adopt_browser_account(account)
            return account.is_logged_in
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
        if self._context is None:
            return
        try:
            cookies = await self._context.cookies()
            # Keep the requests-side reader aligned with the account currently
            # active in Chrome. The direct mutation itself still runs inside the
            # browser and therefore uses that same cookie jar automatically.
            replacer = getattr(self.api, "replace_cookies", None)
            if callable(replacer):
                replacer(cookies)
            # A dedicated real-Chrome profile persists its own cookies. Only the
            # bundled fallback needs the encrypted application copy.
            if self._owns_browser:
                self._save_cookies(cookies)
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
        self, target_date: str, target_time: str, reservation_data, dev_mode: bool
    ) -> tuple[str, str]:
        page = self._page
        if page is None:
            return "error", "브라우저가 준비되지 않았습니다"

        self._dialog_state["message"] = ""
        hour, _, minute = target_time.partition(":")
        target_minutes = int(hour) * 60 + int(minute or 0)

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
                    f"{target_time} 버튼이 화면에 없습니다 "
                    f"(시간표 {rendered}개는 렌더링됨)"
                )
            if not match.get("clickable"):
                # The page is the authority here: the schedule API can report
                # stock while the rendered slot already says 매진.
                return "taken", (
                    f"{target_time} 선택할 수 없는 상태입니다 · "
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
                return "retry", f"{target_time} 클릭이 반영되지 않았습니다"
            timing.mark("슬롯선택")

            quantity_ok, quantity_detail = await self._set_browser_booking_count(
                reservation_data
            )
            if not quantity_ok:
                return "taken", quantity_detail
            timing.mark("수량선택")

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
                'button:has-text("동의하고 결제하기"), '
                'a:has-text("동의하고 예약하기"), a:has-text("동의하고 결제하기")'
            ).first
            try:
                await submit.wait_for(state="visible", timeout=4000)
            except Exception:
                return "retry", "최종 예약·결제 버튼을 찾지 못했습니다"

            async def enabled():
                classes = (await submit.get_attribute("class")) or ""
                return "disabled" not in classes.lower()

            if not await self._poll_for(enabled, timeout=3.0, interval=0.025):
                return "retry", "최종 예약·결제 버튼이 계속 비활성 상태입니다"
            timing.mark("버튼활성")
            self.log(f"[정보] 제출 준비 완료 · {timing.summary()}", "info")

            is_npay = await self._is_npay_submission(submit)
            if is_npay:
                self.log(
                    "[정보] 네이버 선결제형 확인 · Npay 결제 단계로 진행합니다.",
                    "info",
                )
            else:
                self.log(
                    "[정보] 네이버 예약 완료형 확인 · 예약번호 생성으로 완료 처리합니다.",
                    "info",
                )
            if dev_mode and not is_npay:
                # The debug dump is deliberately after the timing report so it
                # cannot inflate the measurement of the real critical path.
                await self._dump_debug(page)
                self.log(
                    "[완료] [개발자 테스트] '동의하고 예약하기' 직전에 멈췄습니다. "
                    "제출하지 않습니다.",
                    "success",
                )
                return "dev", ""

            if is_npay:
                return await self._submit_npay(
                    submit,
                    dev_mode=dev_mode,
                )

            await submit.click()
            self.log("🚀 '동의하고 예약하기' 클릭", "warning")
            return await self._verify_result()

        except Exception as exc:
            if self.stop_event.is_set():
                return "error", "중지됨"
            return "retry", f"{type(exc).__name__}: {str(exc)[:120]}"

    async def _is_npay_submission(self, submit) -> bool:
        """Classify the live action as checkout or booking-only/post-payment.

        ``isNPayUsed`` also appears on some on-site/post-payment products, so the
        final button rendered by Naver is the authoritative signal.  Metadata is
        only a fallback when the live label cannot be read.
        """
        try:
            text = re.sub(
                r"\s+", " ", await submit.inner_text() or ""
            ).strip().lower()
        except Exception:
            text = ""
        if "결제" in text:
            return True
        if "예약" in text:
            return False
        if self._api_preparation is not None and self._api_preparation.ready:
            return self._api_preparation.requires_checkout
        if isinstance(self._api_biz_item, dict):
            value = self._api_biz_item.get("isNPayUsed")
            if value is not None:
                return parse_bool_flag(value)
        return "npay" in text or "naver pay" in text

    @staticmethod
    def _is_submit_booking_response(response) -> bool:
        try:
            if "/graphql" not in str(response.url):
                return False
            post_data = response.request.post_data or ""
            return "submitBooking" in post_data
        except Exception:
            return False

    @staticmethod
    def _normalize_payment_url(raw_url: Any) -> str:
        if isinstance(raw_url, dict):
            raw_url = raw_url.get("pc") or raw_url.get("mobile") or ""
        return str(raw_url or "")

    @staticmethod
    def _parse_submit_booking_response(payload: Any) -> tuple[str, str, str]:
        """Return booking id, trusted navigation candidate, and server error."""
        documents = payload if isinstance(payload, list) else [payload]
        for document in documents:
            if not isinstance(document, dict):
                continue
            node = (document.get("data") or {}).get("submitBooking")
            if isinstance(node, dict):
                booking_id = str(node.get("bookingId") or "")
                raw_url = NaverEngine._normalize_payment_url(node.get("url"))
                return booking_id, raw_url, ""
            errors = document.get("errors") or []
            messages = [
                str(error.get("message") or "")
                for error in errors
                if isinstance(error, dict) and error.get("message")
            ]
            if messages:
                return "", "", " · ".join(messages)[:240]
        return "", "", ""

    @staticmethod
    def _is_npay_url(url: str) -> bool:
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return host == "pay.naver.com" or host.endswith(".pay.naver.com")

    @staticmethod
    def _is_trusted_booking_resume_url(url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "").lower()
        except Exception:
            return False
        return (
            (host == "booking.naver.com" or host.endswith(".booking.naver.com"))
            and ("booking" in path or "/my/" in path)
        ) or (
            host == "m.place.naver.com" and "/my/" in path
        )

    async def _submit_npay(
        self, submit, *, dev_mode: bool
    ) -> tuple[str, str]:
        """Create the temporary booking, then drive the official Npay order page."""
        page = self._page
        if page is None:
            return "error", "브라우저가 준비되지 않았습니다"

        response = None
        clicked = False
        capture_error = ""
        try:
            async with page.expect_response(
                self._is_submit_booking_response,
                timeout=self.NAVIGATION_TIMEOUT_MS,
            ) as response_info:
                await submit.click()
                clicked = True
            response = await response_info.value
        except Exception as exc:
            capture_error = f"{type(exc).__name__}: {str(exc)[:100]}"
            if not clicked:
                try:
                    await submit.click()
                    clicked = True
                except Exception as click_exc:
                    return "retry", f"결제 예약 버튼 클릭 실패: {click_exc}"

        self.log("🚀 '동의하고 결제하기' 클릭", "warning")

        booking_id = ""
        payment_url = ""
        server_error = ""
        if response is not None:
            try:
                booking_id, payment_url, server_error = (
                    self._parse_submit_booking_response(await response.json())
                )
            except Exception as exc:
                capture_error = f"응답 해석 실패: {type(exc).__name__}"

        if server_error:
            return self._classify(server_error), server_error

        return await self._continue_npay_checkout(
            booking_id=booking_id,
            payment_url=payment_url,
            dev_mode=dev_mode,
            navigate_immediately=False,
            capture_error=capture_error,
        )

    async def _continue_npay_checkout(
        self,
        *,
        booking_id: str,
        payment_url: str,
        dev_mode: bool,
        navigate_immediately: bool,
        capture_error: str = "",
    ) -> tuple[str, str]:
        """Continue an Npay hold created by either API or browser submission."""
        if booking_id:
            self._npay_booking_id = booking_id
            self.log(
                f"🎯 Npay 예약 자리 임시 선점 성공 · 예약번호 {booking_id} · "
                "결제를 완료해야 최종 확정됩니다.",
                "success",
            )

        if self.stop_event.is_set():
            return "stopped", "중지됨"

        payment_page = (
            await self._navigate_to_npay_page(payment_url)
            if navigate_immediately
            else await self._wait_for_npay_page(payment_url)
        )
        if self.stop_event.is_set():
            return "stopped", "중지됨"
        if payment_page is None:
            notice = await self._page_notice()
            if notice and not booking_id:
                return self._classify(notice), notice
            if booking_id:
                return (
                    "payment",
                    f"예약번호 {booking_id} 임시 선점 완료 · 결제 페이지 이동을 "
                    "확인하지 못했습니다. 열린 Chrome에서 직접 확인해주세요.",
                )
            return (
                "unknown",
                "결제 제출 결과를 확인하지 못했습니다"
                + (f" ({capture_error})" if capture_error else ""),
            )

        self._page = payment_page
        selected, selection_detail = await self._select_npay_money(payment_page)
        if self.stop_event.is_set():
            return "stopped", "중지됨"
        if not selected:
            self.log(
                f"[경고] Npay 머니를 자동 선택하지 못했습니다 · {selection_detail}",
                "warning",
            )
            return (
                "payment",
                f"예약번호 {booking_id or '확인 필요'} 임시 선점 완료 · "
                "Npay 머니를 직접 선택하고 결제해주세요.",
            )

        self.log(f"[정보] Npay 머니 선택 완료 · {selection_detail}", "success")
        pay_button, button_text = await self._find_npay_pay_button(payment_page)
        if self.stop_event.is_set():
            return "stopped", "중지됨"
        if pay_button is None:
            return (
                "payment",
                f"예약번호 {booking_id or '확인 필요'} 임시 선점 완료 · "
                "최종 결제 버튼을 찾지 못해 화면을 유지합니다.",
            )

        if dev_mode:
            await self._dump_debug(payment_page, "naver_npay_checkout_debug.html")
            self.log(
                f"[완료] [개발자 테스트] Npay 머니를 선택하고 "
                f"'{button_text}' 직전에 멈췄습니다. 결제하지 않습니다. "
                f"예약번호 {booking_id or '확인 필요'}는 임시 선점 상태입니다.",
                "success",
            )
            return "dev", ""

        try:
            await pay_button.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            await pay_button.click(timeout=5000)
        except Exception as exc:
            return (
                "payment",
                f"예약번호 {booking_id or '확인 필요'} 임시 선점 완료 · "
                f"최종 결제 버튼 클릭 실패 ({type(exc).__name__}) · 직접 눌러주세요.",
            )

        self.log(f"💳 Npay 머니 '{button_text}' 클릭", "warning")
        return (
            "payment",
            f"예약번호 {booking_id or '확인 필요'} · Npay 결제 요청을 전송했습니다. "
            "추가 비밀번호·본인인증이 표시되면 Chrome에서 완료해주세요.",
        )

    async def _navigate_to_npay_page(self, payment_url: str):
        """Resume either a direct Npay URL or a reconciled booking-detail hold."""
        if self._page is None:
            return None
        direct_npay = self._is_npay_url(payment_url)
        booking_resume = self._is_trusted_booking_resume_url(payment_url)
        if not direct_npay and not booking_resume:
            return None
        try:
            await self._page.goto(
                payment_url, wait_until="domcontentloaded", timeout=10000
            )
        except Exception:
            return None
        try:
            current_url = self._page.url or ""
        except Exception:
            current_url = ""
        if self._is_npay_url(current_url):
            return self._page
        if direct_npay:
            return None

        # MY플레이스 gives an authenticated booking-detail URL rather than the
        # one-time Npay order URL.  Follow only an explicit payment-resume control
        # on that confirmed booking; this does not create another reservation.
        exact_payment = re.compile(
            r"^\s*(?:네이버페이\s*)?(?:결제하기|결제\s*계속|결제\s*진행)\s*$"
        )
        candidates = (
            self._page.locator("a[href*='pay.naver.com']"),
            self._page.get_by_role("button", name=exact_payment),
            self._page.get_by_role("link", name=exact_payment),
        )
        for group in candidates:
            try:
                count = min(await group.count(), 3)
            except Exception:
                continue
            for index in range(count):
                control = group.nth(index)
                try:
                    if not await control.is_visible():
                        continue
                    href = await control.get_attribute("href")
                    if href:
                        candidate_url = urllib.parse.urljoin(current_url, href)
                        if self._is_npay_url(candidate_url):
                            await self._page.goto(
                                candidate_url,
                                wait_until="domcontentloaded",
                                timeout=10000,
                            )
                            return self._page
                    await control.click(timeout=1500)
                    return await self._wait_for_npay_page("")
                except Exception:
                    continue
        return None

    async def _wait_for_npay_page(self, payment_url: str = ""):
        deadline = time.monotonic() + self.NPAY_PAGE_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not self.stop_event.is_set():
            pages = []
            if self._context is not None:
                try:
                    pages.extend(list(self._context.pages))
                except Exception:
                    pass
            if self._page is not None and self._page not in pages:
                pages.append(self._page)

            for candidate in reversed(pages):
                try:
                    url = candidate.url or ""
                except Exception:
                    continue
                if not self._is_npay_url(url):
                    continue
                try:
                    await candidate.bring_to_front()
                    await candidate.wait_for_load_state(
                        "domcontentloaded", timeout=5000
                    )
                except Exception:
                    pass
                return candidate
            await asyncio.sleep(0.05)

        # The mutation already created a booking. If Naver returned a trusted
        # payment URL but its own redirect failed, use that exact URL once instead
        # of submitting the booking again and risking a duplicate.
        if self.stop_event.is_set():
            return None
        if self._page is not None and self._is_npay_url(payment_url):
            try:
                await self._page.goto(
                    payment_url, wait_until="domcontentloaded", timeout=10000
                )
                return self._page
            except Exception:
                pass
        return None

    async def _select_npay_money(self, page) -> tuple[bool, str]:
        pattern = re.compile(
            r"(?:N\s*pay|네이버\s*페이|네이버페이)\s*머니", re.IGNORECASE
        )
        deadline = time.monotonic() + self.NPAY_CONTROL_TIMEOUT_SECONDS
        last_detail = "결제수단이 아직 표시되지 않았습니다"

        while time.monotonic() < deadline and not self.stop_event.is_set():
            # Accessible radio names are the most stable signal and work whether
            # the visual label says Npay or 네이버페이.
            try:
                radios = page.get_by_role("radio", name=pattern)
                count = min(await radios.count(), 8)
                for index in range(count):
                    radio = radios.nth(index)
                    if not await radio.is_visible():
                        continue
                    try:
                        if not await radio.is_checked():
                            await radio.click(timeout=self.NPAY_ACTION_TIMEOUT_MS)
                    except Exception:
                        if self.stop_event.is_set():
                            return False, "중지 요청"
                        await radio.click(timeout=self.NPAY_ACTION_TIMEOUT_MS)
                    if self.stop_event.is_set():
                        return False, "중지 요청"
                    if await self._npay_money_selected(page):
                        return True, "접근성 라디오 상태 확인"
                    last_detail = "Npay 머니 라디오를 눌렀지만 선택 상태를 확인하지 못했습니다"
            except Exception:
                if self.stop_event.is_set():
                    return False, "중지 요청"
                pass

            # Some Npay builds hide the native radio. Click the visible label/text
            # and still require a checked/aria-checked/selected state afterward.
            for candidate_factory in (
                lambda: page.locator("label").filter(has_text=pattern),
                lambda: page.get_by_text(pattern),
            ):
                try:
                    candidates = candidate_factory()
                    count = min(await candidates.count(), 12)
                    for index in range(count):
                        candidate = candidates.nth(index)
                        if not await candidate.is_visible():
                            continue
                        await candidate.click(timeout=self.NPAY_ACTION_TIMEOUT_MS)
                        if self.stop_event.is_set():
                            return False, "중지 요청"
                        if await self._npay_money_selected(page):
                            return True, "화면 결제수단 선택 상태 확인"
                        last_detail = (
                            "Npay 머니 항목을 눌렀지만 선택 상태를 확인하지 못했습니다"
                        )
                        break
                except Exception:
                    if self.stop_event.is_set():
                        return False, "중지 요청"
                    continue

            await asyncio.sleep(0.1)
        return False, last_detail

    @staticmethod
    async def _npay_money_selected(page) -> bool:
        script = r"""() => {
            const rx = /(?:N\s*pay|네이버\s*페이|네이버페이)\s*머니/i;
            const textOf = (el) => ((el && el.innerText) || '').replace(/\s+/g, ' ');
            const controls = Array.from(document.querySelectorAll(
                'input[type="radio"], [role="radio"], [aria-checked]'));
            for (const control of controls) {
                const scope = control.closest('label, li, article, section, div') || control;
                if (!rx.test(textOf(scope))) continue;
                if (control.checked || control.getAttribute('aria-checked') === 'true') return true;
                const cls = ((control.className || '') + ' ' + (scope.className || '')).toLowerCase();
                if (/(^|[\s_-])(selected|checked|active)([\s_-]|$)/.test(cls)) return true;
                if (control.getAttribute('data-selected') === 'true'
                    || scope.getAttribute('data-selected') === 'true') return true;
            }
            return false;
        }"""
        try:
            return bool(await page.evaluate(script))
        except Exception:
            return False

    async def _find_npay_pay_button(self, page):
        pattern = re.compile(r"^(?:[\d,]+\s*원\s*)?결제하기$", re.IGNORECASE)
        deadline = time.monotonic() + self.NPAY_CONTROL_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not self.stop_event.is_set():
            pools = []
            try:
                pools.append(page.get_by_role("button", name=pattern))
            except Exception:
                pass
            try:
                pools.append(page.locator("button").filter(has_text=pattern))
            except Exception:
                pass

            best = None
            best_text = ""
            best_score = -1
            for pool in pools:
                try:
                    count = min(await pool.count(), 12)
                except Exception:
                    continue
                for index in range(count):
                    if self.stop_event.is_set():
                        return None, "결제하기"
                    candidate = pool.nth(index)
                    try:
                        if not await candidate.is_visible() or not await candidate.is_enabled():
                            continue
                        aria_disabled = await candidate.get_attribute("aria-disabled")
                        classes = (await candidate.get_attribute("class") or "").lower()
                        if aria_disabled == "true" or "disabled" in classes:
                            continue
                        text = re.sub(r"\s+", " ", await candidate.inner_text()).strip()
                    except Exception:
                        continue
                    if not pattern.match(text):
                        continue
                    score = 2 if re.search(r"[\d,]+\s*원", text) else 1
                    if score > best_score:
                        best = candidate
                        best_text = text
                        best_score = score
            if best is not None:
                return best, best_text
            await asyncio.sleep(0.1)
        return None, "결제하기"

    async def _monitor_npay_completion(self) -> str:
        auth_reported = False
        while not self.stop_event.is_set():
            pages = []
            if self._context is not None:
                try:
                    pages.extend(list(self._context.pages))
                except Exception:
                    pass
            if self._page is not None and self._page not in pages:
                pages.append(self._page)
            if not pages:
                return ""

            for page in reversed(pages):
                try:
                    url = page.url or ""
                except Exception:
                    url = ""
                if (
                    page is not self._page
                    and self._npay_booking_id
                    and self._npay_booking_id not in url
                ):
                    continue
                try:
                    body = await page.locator("body").inner_text(timeout=1000)
                except Exception:
                    body = ""

                success_text = (
                    "결제가 완료", "결제 완료", "예약이 완료", "예약 완료",
                    "예약되었습니다",
                )
                try:
                    parsed = urllib.parse.urlparse(url)
                    host = (parsed.hostname or "").lower()
                    path = parsed.path.lower()
                except Exception:
                    host = ""
                    path = ""
                success_by_url = (
                    host.endswith("booking.naver.com")
                    and ("booking-detail" in path or "/my/bookings/" in path)
                ) or (
                    self._is_npay_url(url)
                    and ("/complete" in path or "/completion" in path)
                )
                if success_by_url or any(
                    token in body for token in success_text
                ):
                    self._page = page
                    suffix = (
                        f"예약번호 {self._npay_booking_id}"
                        if self._npay_booking_id else url
                    )
                    return suffix or "완료 화면 확인"

                if not auth_reported and any(
                    token in body
                    for token in ("결제 비밀번호", "본인인증", "비밀번호 입력", "생체인증")
                ):
                    auth_reported = True
                    self._page = page
                    self.log(
                        "[정보] Npay 본인인증이 필요합니다. 열린 Chrome에서 인증하면 "
                        "완료 상태를 계속 확인합니다.",
                        "warning",
                    )
            await asyncio.sleep(self.NPAY_MONITOR_INTERVAL_SECONDS)
        return ""

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
        else:
            # Only a page believed to be warm is worth waiting on. When the last
            # load produced no timetable, this wait is a second of the critical
            # path spent confirming what is already known, so it is cut to a
            # glance that still absorbs a render finishing mid-turn.
            budget = 1200 if self._warmed_for_date else 200
            if not await self._wait_for_timetable(timeout_ms=budget):
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

    async def _set_browser_booking_count(
        self, reservation_data
    ) -> tuple[bool, str]:
        """Match Naver's visible +/- ticket control to the requested people."""
        page = self._page
        if page is None:
            return False, "브라우저가 준비되지 않았습니다"

        expected_quantity = bool(
            self._api_preparation and self._api_preparation.quantity_mode
        )
        try:
            state = await page.evaluate(BOOKING_QUANTITY_SCRIPT, "state")
        except Exception:
            state = None

        if (not state or not state.get("present")) and expected_quantity:
            state = await self._poll_for(
                lambda: page.evaluate(BOOKING_QUANTITY_SCRIPT, "state"),
                timeout=1.0,
                interval=0.02,
            )
        if not state or not state.get("present"):
            return True, "수량 선택이 없는 단일 슬롯 상품"

        desired = max(
            1,
            int(re.sub(r"\D", "", str(reservation_data.get("people") or "")) or 1),
        )
        current = int(state.get("current") or 0)
        if current <= 0:
            return False, "티켓 수량 선택기의 현재 값을 확인하지 못했습니다"

        for _ in range(100):
            if current == desired:
                groups = int(state.get("groups") or 1)
                suffix = f" · 가격 종류 {groups}개 중 기본 항목" if groups > 1 else ""
                self.log(
                    f"[정보] 예매 티켓 수량 {desired}매 설정 완료{suffix}",
                    "success",
                )
                return True, f"티켓 수량 {desired}매"

            action = "plus" if current < desired else "minus"
            disabled_key = "plusDisabled" if action == "plus" else "minusDisabled"
            if state.get(disabled_key):
                return False, (
                    f"요청 수량 {desired}매를 설정할 수 없습니다 · "
                    f"화면에서 가능한 현재 수량 {current}매"
                )
            try:
                clicked = await page.evaluate(BOOKING_QUANTITY_SCRIPT, action)
            except Exception:
                clicked = None
            if not clicked or not clicked.get("clicked"):
                return False, f"티켓 수량 {desired}매 조정 버튼을 누르지 못했습니다"

            previous = current

            async def changed():
                updated = await page.evaluate(BOOKING_QUANTITY_SCRIPT, "state")
                if updated and int(updated.get("current") or 0) != previous:
                    return updated
                return None

            state = await self._poll_for(changed, timeout=0.8, interval=0.015)
            if not state:
                return False, "티켓 수량 변경이 화면에 반영되지 않았습니다"
            current = int(state.get("current") or 0)

        return False, "티켓 수량 조정 횟수가 안전 제한을 초과했습니다"

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

        if not self._custom_form_answers:
            return

        occurrences: dict[str, int] = {}
        for answer in self._custom_form_answers:
            occurrence = occurrences.get(answer.title, 0)
            occurrences[answer.title] = occurrence + 1
            values = list(answer.selected_values) or [answer.value]
            parts = range(len(values)) if answer.kind == "BIRTH" else range(1)
            completed = True

            for part in parts:
                payload = {
                    "index": answer.index,
                    "title": answer.title,
                    "kind": answer.kind,
                    "value": answer.value,
                    "values": values,
                    "occurrence": occurrence,
                    "part": part,
                }
                try:
                    result = await page.evaluate(CUSTOM_FORM_CONTROL_SCRIPT, payload)
                except Exception as exc:
                    result = {"state": "error", "detail": type(exc).__name__}

                state = result.get("state") if isinstance(result, dict) else "missing"
                if state == "filled":
                    continue
                if state != "opened":
                    self.log(
                        f"[경고] 추가 입력 '{answer.title}' 화면 컨트롤을 "
                        f"채우지 못했습니다. ({state})",
                        "warning",
                    )
                    completed = False
                    break

                wanted = values[min(part, len(values) - 1)]
                picked = await self._poll_for(
                    lambda wanted=wanted: page.evaluate(PICK_OPTION_SCRIPT, wanted),
                    timeout=2.0,
                    interval=0.02,
                )
                if picked is not True:
                    self.log(
                        f"[경고] 추가 입력 '{answer.title}'의 '{wanted}' "
                        f"선택지를 찾지 못했습니다. 화면 선택지: {picked}",
                        "warning",
                    )
                    completed = False
                    break

            if completed:
                sensitive = re.search(
                    r"이름|성명|연락처|전화|휴대폰|이메일|생년월일|birth|phone|name|email",
                    answer.title,
                    re.I,
                )
                shown_value = "개인정보" if sensitive else answer.value
                self.log(
                    f"[정보] 추가 입력 '{answer.title}' → "
                    f"'{shown_value}' 화면 입력 완료",
                    "info",
                )

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
            write_redacted_debug_text(path, await page.content())
            self.log(
                f"[정보] [디버그] 민감정보 제거 요청 페이지 HTML 저장: {path}",
                "info",
            )
        except Exception as exc:
            self.log(
                f"[경고] 디버그 HTML 저장 실패: {format_exception(exc)}",
                "warning",
            )

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
