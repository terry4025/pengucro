"""Keyescape reservation engine.

Flow
----
1. The program enters Step 2 (``reservation2.php``) and fills in the reservation
   details -- name, the three phone fields, party size, payment method and the
   consent checkboxes.
2. With YesCaptcha enabled, an API token is acquired shortly before opening and
   verified in the actual form before submission. With it disabled, the program
   only observes a token the user solved manually and never clicks or patches the
   widget.
3. The program waits on the site's server clock.  One standby page may use a
   schedule id that was verified across multiple published dates; the remaining
   pages retain the live target-date lookup as a safety net.

Why both the server clock and a live fallback are retained
-----------------------------------------------------------
``/controller/run_proc.php`` with ``t=get_theme_time`` returns one row per time
slot with an ``enable`` field; the site's own front-end renders ``enable === 'N'``
as a disabled radio button. It does not reliably prove that a future date's
booking window is open.  The clock remains the authority for *when* to submit.
Repeated public schedules showed that slot ids belong to a theme/schedule/time
row rather than to one calendar date. Monday through Thursday still require two
matching dates; Friday through Sunday may use one fresh full schedule only when
the site's explicit A/B/C/D schedule-group marker matches the target weekday.

Branch opening times are still read, but only to pace the polling and to tell
the user how long they have to wait. Nothing depends on them being correct.
"""

import asyncio
import ctypes
import hashlib
import json
import re
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from engines import browser_session
from engines.base_engine import BaseEngine
from engines.keyescape_coordination import SharedServerClock, SharedSlotLookup
from engines.keyescape_schedule_cache import remember_slot_template
from engines.server_clock import ServerClock
from engines.yescaptcha_client import YesCaptchaClient, DEFAULT_SOFT_ID
from pengucro.diagnostics import write_redacted_debug_text
from pengucro.models import BookingResult, coerce_bool
from pengucro.storage import append_history, data_path, load_json, save_json


# '-예약 오픈 시간은 10:00분 입니다.' and similar wordings found in the branch
# notice text. Verified against all 14 branches: 12 matched the table below,
# one (무비무드 전주) proved the table wrong, one had no such line.
OPEN_TIME_PATTERN = re.compile(
    r"예약\s*오픈\s*시간[^0-9]{0,12}(\d{1,2})\s*(?:[:시]\s*(\d{1,2})?)?"
)
# Returned by the booking endpoint when the window has not opened, e.g.
# '예약가능시간이 아닙니다. 예약오픈시간 : 11:00'. This is the most authoritative
# source of the open time, so a rejection is used to correct the schedule.
SERVER_OPEN_TIME_PATTERN = re.compile(r"예약\s*오픈\s*시간[^0-9]{0,6}(\d{1,2})\s*:\s*(\d{2})")

# Text the site paints over document.body once its devtools detector fires.
# Used to recognise a wiped page instead of polling a corpse forever.
DEVTOOLS_BLOCK_MARKER = "개발자 도구 사용이 금지"

# reCAPTCHA v2 site key as rendered by reservation2.php
# (<span id="captcha" class="g-recaptcha" data-sitekey="...">). Only a fallback:
# the key is read off the live page so a rotation does not silently produce
# tokens for the wrong widget. Kept in sync with reference/keyescape.
FALLBACK_SITEKEY = "6Le0ObMqAAAAAF7j701m2aQsHLQFe_KDYpKvw3jQ"

# Neutralises the site's devtools guard *before* its own scripts run.
#
# Every reservation page (step 1, step 2 and the completion page) loads
# cdnjs' devtools-detector and replaces document.body with a black
# "개발자 도구 사용이 금지되어 있습니다." screen when the detector reports open.
# Attaching over the Chrome DevTools protocol is a genuine positive for that
# library, so the detector cannot be avoided -- only disarmed.
#
# The previous defence rewrote the CDN response through page.route. That fails
# whenever the script comes out of the persistent Chrome profile's disk cache
# (no network request, so no interception) and never applied to popups or tabs
# the site opened itself. Pre-defining the globals in an init script works
# regardless of caching and is inherited by every page and frame in the context.
DEVTOOLS_GUARD_SCRIPT = r"""
(() => {
    const stub = {
        addListener() {},
        removeListener() {},
        launch() {},
        stop() {},
        isLaunch() { return false; },
        isOpen: false,
    };
    // A setter that ignores writes, rather than writable:false: the real
    // library's UMD wrapper assigns window.devtoolsDetector and would throw a
    // TypeError under strict mode on a read-only property.
    try {
        Object.defineProperty(window, 'devtoolsDetector', {
            configurable: false,
            get() { return stub; },
            set() {},
        });
    } catch (e) { /* already defined -- nothing safe to do */ }

    // The inline guard also calls blockDevTools() from document.onkeydown for
    // F12 / Ctrl+U / Ctrl+Shift+I. That handler lives in a closure, so the only
    // way to reach it is to refuse the assignment itself.
    try {
        Object.defineProperty(document, 'onkeydown', {
            configurable: false,
            get() { return null; },
            set() {},
        });
    } catch (e) { /* ignore */ }
})();
"""


def parse_open_time(text):
    """Extract (hour, minute) from notice text, or None."""
    if not text:
        return None
    match = OPEN_TIME_PATTERN.search(str(text).replace("\r", " ").replace("\n", " "))
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


KST = timezone(timedelta(hours=9))

# Fallback opening times (zizum_num -> (hour, minute), KST), used only when the
# time cannot be parsed out of the branch notice.
#
# These were originally transcribed from a screenshot and had already drifted: a
# sweep of all 14 branches found 12 agreeing with the live notice, 무비무드 전주
# listed here as 13:30 while the site says 18:00, and 에버랜드 with no parseable
# line at all. The live notice therefore always wins.
BRANCH_OPEN_TIMES = {
    '19': (10, 0),   # LOG_IN 1
    '20': (10, 0),   # LOG_IN 2
    '14': (10, 0),   # 강남 더오름
    '16': (10, 0),   # 우주라이크
    '18': (10, 30),  # 메모리컴퍼니
    '23': (11, 0),   # 후즈데어
    '22': (11, 30),  # STATION
    '25': (13, 30),  # 무비무드
    '3': (18, 0),    # 강남점
    '9': (18, 0),    # 부산점
    '7': (18, 0),    # 전주점
    '10': (20, 0),   # 홍대점
    '26': (10, 0),   # 에버랜드 (notice has no parseable line; unverified)
    '29': (18, 0),   # 무비무드 전주 (corrected from 13:30 per the live notice)
}


class KeyescapeEngine(BaseEngine):
    # Timing, all measured on the *server's* clock.
    #
    # An earlier design polled get_theme_time and treated enable == 'Y' as the
    # open signal. That was wrong: probing the same slot on two dates showed
    # enable='N' for a date whose window was open (sold out) and enable='Y' for
    # one that had not opened yet (nobody could book it). enable means "not
    # taken", not "bookable". The window is enforced only on submission, and the
    # booking endpoint rejects any call without a captcha token ('잘못된 접근',
    # reproduced with and without a session), so it cannot be polled either --
    # each attempt would burn a token, since the site itself calls
    # grecaptcha.reset() on failure. Hence a clock trigger.
    CAPTCHA_PROMPT_LEAD = 90.0      # keep manual solves inside a safe token window
    FINAL_SYNC_LEAD = 20.0          # last clock re-sync before firing
    RESYNC_INTERVAL = 120.0         # periodic re-sync while waiting
    FIRE_LEAD = 0.0                 # the server now rejects even slightly early calls
    POLL_IDLE_SECONDS = 1.0
    POLL_NEAR_SECONDS = 0.02

    # reCAPTCHA v2 tokens are single use and expire in about two minutes.
    CAPTCHA_TTL_SECONDS = 105       # leave safety margin under Google's 120 s limit
    CAPTCHA_WARN_SECONDS = 25
    # Start close enough to the open moment that a fast result is submitted
    # within YesCaptcha's recommended 60-second window. A slower task may finish
    # after opening; the watch loop submits it immediately when it arrives.
    CAPTCHA_SOLVE_LEAD = 70.0
    CAPTCHA_SOLVE_LEAD_MIN = 70.0
    CAPTCHA_SOLVE_LEAD_MAX = 90.0
    CAPTCHA_SOLVE_EXTRA_MARGIN = 25.0
    CAPTCHA_TIMING_FILE = "keyescape_captcha_timing.json"
    CAPTCHA_TIMING_SAMPLE_LIMIT = 30
    CAPTCHA_SOLVE_TIMEOUT = 120
    # Re-request once the token has less than this much life left.
    CAPTCHA_REFRESH_MARGIN = 30.0
    YESCAPTCHA_MAX_FAILURES = 4
    YESCAPTCHA_RETRY_COOLDOWN = 5.0
    # Manual mode only: the widget is poked at most this often, this many times.
    ANCHOR_CLICK_COOLDOWN = 25.0
    ANCHOR_CLICK_MAX = 2

    MAX_STANDBY_PAGES = 3
    SUBMIT_MAX_ATTEMPTS = 5
    SUBMISSION_RECONCILE_SECONDS = 4.0
    SIBLING_SUCCESS_GRACE_SECONDS = 0.25
    PLACEHOLDER_SLOT_ID = "9999"
    SLOT_OPEN_RETRY_SECONDS = 0.12
    SLOT_RETRY_MIN_SECONDS = 0.08
    SLOT_RETRY_MAX_SECONDS = 0.20
    SLOT_HEDGE_DELAY_SECONDS = 0.025
    SLOT_HEDGE_MIN_SECONDS = 0.015
    SLOT_HEDGE_MAX_SECONDS = 0.060
    SLOT_READ_LEAD_SECONDS = 0.450
    SLOT_READ_LEAD_MIN_SECONDS = 0.010
    SLOT_READ_LEAD_MAX_SECONDS = 0.650
    SLOT_PREWARM_LEAD_SECONDS = 3.0
    FINAL_QUIET_LEAD_SECONDS = 2.0
    TIMING_HISTORY_FILE = "keyescape_timing.json"
    SLOT_TEMPLATE_FILE = "keyescape_slot_templates.json"
    TIMING_SAMPLE_LIMIT = 20
    SLOT_TEMPLATE_LIMIT = 40
    SLOT_TEMPLATE_REFRESH_TTL_SECONDS = 1800.0
    TRUSTED_TEMPLATE_MAX_AGE_DAYS = 21
    TRUSTED_SINGLE_TEMPLATE_MAX_AGE_DAYS = 8
    TRUSTED_FIRE_EXTRA_SECONDS = 0.005
    SHARED_SLOT_WAIT_SECONDS = 1.5
    # How many times the step 2 screen may be rebuilt after the site's guard
    # wipes it, before giving up and handing the window to the user.
    MAX_PAGE_RESTORES = 3

    def __init__(self, log_callback, success_callback=None, site_url=None):
        super().__init__(log_callback, success_callback)
        self.browser_thread = None
        self.site_url = (site_url or 'https://www.keyescape.com').rstrip('/')
        self.api_url = f"{self.site_url}/controller/run_proc.php"
        self.reservation_url = f"{self.site_url}/reservation2.php"
        # One session for the whole run. The previous code called
        # requests.post() directly every 0.1 s, paying a fresh TCP + TLS
        # handshake roughly ten times a second.
        self._request_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.site_url}/reservation.php",
        }
        self._session = self._new_site_session()
        # Created lazily by the coordinator only. Page workers keep their own
        # browser/captcha state, but do not waste sockets on an unused hedge.
        self._slot_hedge_session = None
        self._slot_background_tasks: set[asyncio.Task] = set()
        self._last_messages: dict[str, float] = {}

        # config.json switches:
        #   "keyescape_use_real_chrome": false -> always use the throwaway
        #       Playwright profile (previous behaviour)
        #   "keyescape_close_chrome_on_exit": true -> quit the Chrome this engine
        #       started once the run ends; off by default so the profile keeps
        #       building up reputation and the user can inspect the result
        config = load_json("config.json", {})
        if not isinstance(config, dict):
            config = {}
        # HEAD on the reservation page returns no body (0 bytes, ~20 ms), which
        # makes it the cheapest way to read the site's clock.
        self.clock = ServerClock(
            f"{self.site_url}/reservation.php", session=self._session, log=self.log
        )
        self._clock_share = SharedServerClock(self.site_url.lower())
        self._preferred_end_day = 0
        self._last_slot_state = ""
        # Snapshot of the step 2 bootstrap form, used to rebuild the screen if
        # the site's devtools guard ever manages to wipe it.
        self._step_two_html = ""
        # Epoch seconds on the server clock at which the window opens.
        self.open_at = None
        # "keyescape_agree_all": false -> tick only the two mandatory consents and
        # leave the optional marketing opt-in alone (전체동의 will stay unchecked,
        # which the site accepts).
        self._agree_all = bool(config.get("keyescape_agree_all", True))
        self._use_real_chrome = bool(config.get("keyescape_use_real_chrome", True))
        self._close_chrome_on_exit = bool(
            config.get("keyescape_close_chrome_on_exit", False)
        )

        # --- captcha state -------------------------------------------------
        self._sitekey = ""
        self._yc_enabled = False
        self._yc_test_mode = False
        self._yc_test_attempted = False
        self._yc_token_test_only = False
        self._yc_client_key = ""
        self._yc_soft_id = DEFAULT_SOFT_ID
        self._yc_token = ""
        self._yc_token_at = 0.0
        self._yc_task = None
        self._yc_cancel_event = threading.Event()
        self._yc_failures = 0
        self._yc_last_attempt = 0.0
        self._yc_token_submitted = False
        self._yc_profile_key = "default"
        self._captcha_solve_lead = self.CAPTCHA_SOLVE_LEAD
        self._anchor_clicks = 0
        self._anchor_last_click = 0.0
        self._page_count = 1
        self._page_workers = []
        self._winner_page = None
        self._page_success_event = threading.Event()
        self._browser_connection_lost = False
        # One authoritative target-date slot lookup is shared by every standby
        # page.  Only page 1 may use a separately two-date-verified schedule id;
        # placeholder values are never accepted as final values.
        self._live_slot_state = None
        self._trusted_slot_id = ""
        self._trusted_slot_sources: tuple[str, ...] = ()
        self._slot_share = None

    def _new_site_session(self):
        session = requests.Session()
        session.headers.update(self._request_headers)
        return session

    def _sync_server_clock(self, announce=False):
        return self._clock_share.sync(
            self.clock,
            announce=bool(announce),
            max_age=5.0,
            wait_timeout=3.0,
        )

    @staticmethod
    def _captcha_profile_id(client_key: str) -> str:
        if not client_key:
            return "default"
        return hashlib.sha256(client_key.encode("utf-8")).hexdigest()[:12]

    def _load_captcha_solve_lead(self) -> float:
        history = load_json(self.CAPTCHA_TIMING_FILE, {"entries": {}})
        entries = history.get("entries", {}) if isinstance(history, dict) else {}
        samples = entries.get(self._yc_profile_key, []) if isinstance(entries, dict) else []
        valid = []
        for sample in samples if isinstance(samples, list) else []:
            try:
                seconds = float(sample.get("seconds", 0.0))
            except (AttributeError, TypeError, ValueError):
                continue
            if 1.0 <= seconds <= float(self.CAPTCHA_SOLVE_TIMEOUT):
                valid.append(seconds)
        if len(valid) < 3:
            return self.CAPTCHA_SOLVE_LEAD
        valid.sort()
        percentile_index = max(0, min(len(valid) - 1, round((len(valid) - 1) * 0.90)))
        suggested = valid[percentile_index] + self.CAPTCHA_SOLVE_EXTRA_MARGIN
        return min(
            self.CAPTCHA_SOLVE_LEAD_MAX,
            max(self.CAPTCHA_SOLVE_LEAD_MIN, suggested),
        )

    def _remember_captcha_solve_time(self, seconds: float) -> None:
        try:
            elapsed = float(seconds)
        except (TypeError, ValueError):
            return
        if not (1.0 <= elapsed <= float(self.CAPTCHA_SOLVE_TIMEOUT)):
            return
        history = load_json(self.CAPTCHA_TIMING_FILE, {"version": 1, "entries": {}})
        if not isinstance(history, dict):
            history = {"version": 1, "entries": {}}
        entries = history.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            history["entries"] = entries
        samples = entries.get(self._yc_profile_key, [])
        if not isinstance(samples, list):
            samples = []
        samples.append({
            "seconds": round(elapsed, 3),
            "observed_at": datetime.now(KST).isoformat(timespec="seconds"),
        })
        entries[self._yc_profile_key] = samples[-self.CAPTCHA_TIMING_SAMPLE_LIMIT:]
        try:
            save_json(self.CAPTCHA_TIMING_FILE, history)
        except OSError:
            return
        self._captcha_solve_lead = self._load_captcha_solve_lead()

    def _captcha_lead_seconds(self) -> float:
        try:
            return min(
                self.CAPTCHA_SOLVE_LEAD_MAX,
                max(self.CAPTCHA_SOLVE_LEAD_MIN, float(self._captcha_solve_lead)),
            )
        except (TypeError, ValueError):
            return self.CAPTCHA_SOLVE_LEAD

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def read_yescaptcha_settings(reservation_data):
        """Return (enabled, client_key, soft_id) from a payload dict or request.

        Engines are handed ReservationRequest.to_engine_payload(), a plain dict,
        but the same object is occasionally passed straight through in tests, so
        both shapes are accepted. The three sources are tried in order and the
        first one that yields the flag wins -- reading the flag from one source
        and the key from another is what previously produced an "enabled" run
        with an empty key.
        """
        def pick(source):
            if source is None:
                return None
            if isinstance(source, dict):
                if "yescaptcha_enabled" not in source:
                    return None
                enabled = coerce_bool(source.get("yescaptcha_enabled", False))
                key = str(source.get("yescaptcha_client_key", "") or "").strip()
                soft = str(source.get("yescaptcha_soft_id", "") or "").strip()
            else:
                if not hasattr(source, "yescaptcha_enabled"):
                    return None
                enabled = coerce_bool(getattr(source, "yescaptcha_enabled", False))
                key = str(getattr(source, "yescaptcha_client_key", "") or "").strip()
                soft = str(getattr(source, "yescaptcha_soft_id", "") or "").strip()
            return enabled, key, soft or DEFAULT_SOFT_ID

        for source in (
            reservation_data,
            getattr(reservation_data, "raw_data", None),
        ):
            found = pick(source)
            if found is not None:
                return found
        return False, "", DEFAULT_SOFT_ID

    @staticmethod
    def read_yescaptcha_test_mode(reservation_data):
        """Return the immediate one-shot test flag from the same payload source."""
        for source in (
            reservation_data,
            getattr(reservation_data, "raw_data", None),
        ):
            if source is None:
                continue
            if isinstance(source, dict):
                if "yescaptcha_test_mode" in source:
                    return coerce_bool(source.get("yescaptcha_test_mode", False))
            elif hasattr(source, "yescaptcha_test_mode"):
                return coerce_bool(getattr(source, "yescaptcha_test_mode", False))
        return False

    def start_reservation(self, reservation_data, num_threads, is_async=False):
        enabled, client_key, _soft_id = self.read_yescaptcha_settings(reservation_data)
        test_mode = self.read_yescaptcha_test_mode(reservation_data)

        requested = int(num_threads or 1)
        self._page_count = max(1, min(requested, self.MAX_STANDBY_PAGES))
        self._winner_page = None
        self._page_success_event.clear()
        if requested != self._page_count:
            self.log(
                f"[정보] 키이스케이프 동시 페이지 수를 {requested} → "
                f"{self._page_count}로 조정했습니다. (최대 {self.MAX_STANDBY_PAGES})",
                "info",
            )
        if enabled:
            self.log(
                "[YesCaptcha ON] 캡차 자동 해결을 사용합니다."
                + (" · 즉시 테스트 1회 활성화" if test_mode and client_key else "")
                + ("" if client_key else " (경고: API 키가 비어 있어 수동 인증으로 진행합니다)"),
                "info" if client_key else "warning",
            )
        self.log(
            f"키이스케이프 모드: 브라우저 1개에서 {self._page_count}개 페이지를 "
            "핫 스탠바이로 준비합니다. 캡차가 준비된 모든 페이지가 오픈 시각에 동시에 제출합니다.",
            "info",
        )
        # One coordinator thread owns one Playwright connection and all pages.
        # Page-level concurrency happens as asyncio tasks inside that thread.
        super().start_reservation(reservation_data, num_threads=1, is_async=False)

    def make_reservation_thread(self, reservation_data):
        # BaseEngine already owns the worker thread. Running the browser loop
        # directly keeps `is_running` true until Playwright has really exited.
        self.browser_thread = threading.current_thread()
        self._run_browser_booking(reservation_data)

    def _run_browser_booking(self, reservation_data):
        asyncio.run(self._run_browser_booking_async(reservation_data))

    # ------------------------------------------------------------------
    # Logging helper
    # ------------------------------------------------------------------
    def _log_throttled(self, key, message, log_type="info", interval=5.0):
        now = time.monotonic()
        if now - self._last_messages.get(key, 0.0) >= interval:
            self._last_messages[key] = now
            self.log(message, log_type)

    # ------------------------------------------------------------------
    # Site API
    # ------------------------------------------------------------------
    def _post(self, payload, timeout=5.0, session=None):
        active_session = session or self._session
        response = active_session.post(self.api_url, data=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()

    async def _post_async(self, payload, timeout=5.0):
        """Run the blocking HTTP call off the event loop.

        The old code called requests.post() straight from a coroutine, so every
        poll froze the Playwright loop for up to the timeout -- stalling dialog
        handling and captcha observation at the worst possible moment.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._post(payload, timeout))

    async def _fetch_slots(self, target_date, zizum_num, theme_num, end_day=0):
        return await self._fetch_slots_with_session(
            self._session, target_date, zizum_num, theme_num, end_day
        )

    async def _fetch_slots_with_session(
        self, session, target_date, zizum_num, theme_num, end_day=0
    ):
        payload = {
            't': 'get_theme_time',
            'date': target_date,
            'zizumNum': zizum_num,
            'themeNum': theme_num,
            'endDay': '1' if end_day else '0',
        }
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None, lambda: self._post(payload, 5.0, session=session)
        )
        if not data.get("status") or not data.get("data"):
            return []
        return list(data["data"])

    @staticmethod
    def _schedule_group(day) -> str:
        # The live site currently shares one schedule from Monday through
        # Thursday and uses separate row sets for Friday, Saturday and Sunday.
        # Weekend/special rows are never inferred from another weekday.
        return "mon_thu" if day.weekday() <= 3 else f"weekday_{day.weekday()}"

    @staticmethod
    def _expected_schedule_gubun(day) -> str:
        return ("A", "A", "A", "A", "B", "C", "D")[day.weekday()]

    @classmethod
    def _slot_template_gubun(cls, slots) -> str:
        values = {
            str(row.get("gubun", "") or "").strip().upper()
            for row in slots or []
            if isinstance(row, dict)
            and cls._slot_time(row)
            and str(row.get("num", "") or "")
            and str(row.get("num", "") or "") != cls.PLACEHOLDER_SLOT_ID
        }
        values.discard("")
        return next(iter(values)) if len(values) == 1 else ""

    @classmethod
    def _slot_template_payload(cls, slots) -> tuple[str, dict[str, str]]:
        mapping: dict[str, str] = {}
        for row in slots or []:
            stamp = cls._slot_time(row)
            slot_id = str(row.get("num", "") or "") if isinstance(row, dict) else ""
            if stamp and slot_id and slot_id != cls.PLACEHOLDER_SLOT_ID:
                mapping[stamp] = slot_id
        if len(mapping) < 2:
            return "", {}
        canonical = json.dumps(
            sorted(mapping.items()), ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), mapping

    def _remember_slot_template(
        self, target_date, zizum_num, theme_num, slots
    ) -> bool:
        return remember_slot_template(
            self.site_url, target_date, str(zizum_num), str(theme_num), slots
        )

    def _trusted_slot_from_cache(
        self, target_date, target_time, zizum_num, theme_num
    ) -> tuple[str, tuple[str, ...]]:
        try:
            target_day = datetime.strptime(target_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return "", ()
        canonical_site = self.site_url.rstrip("/").lower()
        for suffix in ("/reservation.php", "/reservation2.php"):
            if canonical_site.endswith(suffix):
                canonical_site = canonical_site[:-len(suffix)]
                break
        keys = (
            f"{canonical_site}|{zizum_num}|{theme_num}",
            f"{canonical_site}/reservation.php|{zizum_num}|{theme_num}",
            f"{canonical_site}/reservation2.php|{zizum_num}|{theme_num}",
        )
        cache = load_json(self.SLOT_TEMPLATE_FILE, {"entries": {}})
        entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
        history = []
        if isinstance(entries, dict):
            for key in keys:
                rows = entries.get(key, [])
                if isinstance(rows, list):
                    history.extend(rows)
        candidates = []
        seen_dates = set()
        wanted_group = self._schedule_group(target_day)
        for row in sorted(
            (item for item in history if isinstance(item, dict)),
            key=lambda item: str(item.get("date", "")),
            reverse=True,
        ):
            try:
                source_day = datetime.strptime(
                    str(row.get("date", "")), "%Y-%m-%d"
                ).date()
            except ValueError:
                continue
            if source_day >= target_day or row.get("group") != wanted_group:
                continue
            if (target_day - source_day).days > self.TRUSTED_TEMPLATE_MAX_AGE_DAYS:
                continue
            if source_day in seen_dates:
                continue
            slots = row.get("slots")
            signature = str(row.get("signature", "") or "")
            if not signature or not isinstance(slots, dict):
                continue
            seen_dates.add(source_day)
            candidates.append((
                source_day,
                signature,
                slots,
                str(row.get("gubun", "") or "").strip().upper(),
            ))
            if len(candidates) >= 2:
                break
        if len(candidates) >= 2:
            newest, previous = candidates[0], candidates[1]
            if newest[1] != previous[1]:
                return "", ()
            newest_id = str(newest[2].get(target_time, "") or "")
            previous_id = str(previous[2].get(target_time, "") or "")
            if not newest_id or newest_id != previous_id:
                return "", ()
            return newest_id, (newest[0].isoformat(), previous[0].isoformat())

        # The rolling public window normally contains only one Friday, Saturday
        # and Sunday. Their rows carry an explicit B/C/D group marker, so one
        # recent *complete* schedule can arm page 1 while the other pages retain
        # the live target-date safety path. Mon-Thu never uses this relaxation.
        if len(candidates) == 1 and target_day.weekday() >= 4:
            source_day, _signature, slots, gubun = candidates[0]
            if (
                (target_day - source_day).days
                <= self.TRUSTED_SINGLE_TEMPLATE_MAX_AGE_DAYS
                and gubun == self._expected_schedule_gubun(target_day)
            ):
                slot_id = str(slots.get(target_time, "") or "")
                if slot_id:
                    return slot_id, (source_day.isoformat(),)

        return "", ()

    def _trusted_cache_age_seconds(self, zizum_num, theme_num):
        canonical_site = self.site_url.rstrip("/").lower()
        for suffix in ("/reservation.php", "/reservation2.php"):
            if canonical_site.endswith(suffix):
                canonical_site = canonical_site[:-len(suffix)]
                break
        key = f"{canonical_site}|{zizum_num}|{theme_num}"
        cache = load_json(self.SLOT_TEMPLATE_FILE, {"entries": {}})
        entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
        rows = entries.get(key, []) if isinstance(entries, dict) else []
        newest = None
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                observed = datetime.fromisoformat(str(row.get("observed_at", "")))
                stamp = observed.timestamp()
            except (TypeError, ValueError, OSError):
                continue
            newest = stamp if newest is None else max(newest, stamp)
        return None if newest is None else max(0.0, time.time() - newest)

    async def _prime_trusted_slot_template(
        self, target_date, target_time, zizum_num, theme_num, doing_days
    ) -> tuple[str, tuple[str, ...]]:
        """Observe published dates and prepare the guarded schedule fast path."""
        try:
            target_day = datetime.strptime(target_date, "%Y-%m-%d").date()
            server_day = datetime.fromtimestamp(self.clock.now(), KST).date()
        except (TypeError, ValueError, OSError):
            return "", ()
        cached = self._trusted_slot_from_cache(
            target_date, target_time, zizum_num, theme_num
        )
        cache_age = self._trusted_cache_age_seconds(zizum_num, theme_num)
        if (
            cached[0]
            and cache_age is not None
            and cache_age <= self.SLOT_TEMPLATE_REFRESH_TTL_SECONDS
        ):
            self.log(
                f"[정보] 선택 테마 시간표 캐시 신선도 확인 · {cache_age:.0f}초 전 갱신 · "
                "추가 조회 없이 Fast Path를 준비합니다.",
                "info",
            )
            return cached

        # Refresh every published date for this theme so each real weekday
        # schedule remains available locally after it leaves the site window.
        candidates = [
            server_day + timedelta(days=offset)
            for offset in range(max(0, min(int(doing_days or 0), 8)))
            if server_day + timedelta(days=offset) < target_day
        ]
        for source_day in sorted(candidates, reverse=True):
            try:
                slots = await self._fetch_slots(
                    source_day.isoformat(), zizum_num, theme_num
                )
            except Exception:
                continue
            if slots:
                self._remember_slot_template(
                    source_day.isoformat(), zizum_num, theme_num, slots
                )
        return self._trusted_slot_from_cache(
            target_date, target_time, zizum_num, theme_num
        )

    def _get_slot_hedge_session(self):
        if self._slot_hedge_session is None:
            self._slot_hedge_session = self._new_site_session()
        return self._slot_hedge_session

    @classmethod
    def _timing_parameters(cls, samples):
        valid_rtts = []
        for sample in samples or []:
            try:
                value = float(sample.get("read_rtt_ms", 0)) / 1000.0
            except (AttributeError, TypeError, ValueError):
                continue
            if value > 0:
                valid_rtts.append(value)
        if not valid_rtts:
            return (
                cls.SLOT_HEDGE_DELAY_SECONDS,
                cls.SLOT_OPEN_RETRY_SECONDS,
                cls.SLOT_READ_LEAD_SECONDS,
            )
        typical_rtt = statistics.median(valid_rtts)
        hedge_delay = min(
            cls.SLOT_HEDGE_MAX_SECONDS,
            max(cls.SLOT_HEDGE_MIN_SECONDS, typical_rtt * 0.35),
        )
        retry_delay = min(
            cls.SLOT_RETRY_MAX_SECONDS,
            max(cls.SLOT_RETRY_MIN_SECONDS, typical_rtt),
        )
        read_lead = min(
            cls.SLOT_READ_LEAD_MAX_SECONDS,
            max(cls.SLOT_READ_LEAD_MIN_SECONDS, typical_rtt / 2.0),
        )
        return hedge_delay, retry_delay, read_lead

    @classmethod
    def _cold_start_timing_parameters(cls, initial_rtt):
        """Use a congestion-sized first lead before any opening sample exists."""
        try:
            measured_rtt = max(0.0, float(initial_rtt))
        except (TypeError, ValueError):
            measured_rtt = 0.0
        hedge_delay = min(
            cls.SLOT_HEDGE_MAX_SECONDS,
            max(cls.SLOT_HEDGE_MIN_SECONDS, measured_rtt * 0.35),
        )
        retry_delay = min(
            cls.SLOT_RETRY_MAX_SECONDS,
            max(cls.SLOT_RETRY_MIN_SECONDS, measured_rtt),
        )
        read_lead = min(
            cls.SLOT_READ_LEAD_MAX_SECONDS,
            max(
                cls.SLOT_READ_LEAD_MIN_SECONDS,
                cls.SLOT_READ_LEAD_SECONDS,
                measured_rtt / 2.0,
            ),
        )
        return hedge_delay, retry_delay, read_lead

    def _ensure_parallel_live_fallback(self, already_open: bool) -> bool:
        """Give a one-page trusted run an independent live-query safety page."""
        if not self._trusted_slot_id or already_open or self._page_count != 1:
            return False
        self._page_count = 2
        self.log(
            "[정보] 검증 시간표 빠른 제출 1페이지와 실제 시간표 확인 "
            "1페이지를 병렬 안전 경로로 자동 준비합니다.",
            "info",
        )
        return True

    @staticmethod
    def _timing_key(zizum_num, theme_num, target_time):
        return f"{zizum_num}:{theme_num}:{target_time}"

    def _load_timing_profile(self, zizum_num, theme_num, target_time):
        key = self._timing_key(zizum_num, theme_num, target_time)
        history = load_json(self.TIMING_HISTORY_FILE, {})
        if not isinstance(history, dict):
            history = {}
        entry = history.get(key, {})
        samples = entry.get("samples", []) if isinstance(entry, dict) else []
        hedge_delay, retry_delay, read_lead = self._timing_parameters(samples)
        return key, hedge_delay, retry_delay, read_lead, bool(samples)

    def _remember_slot_timing(self, state):
        if state.get("timing_sample") is not None:
            return
        try:
            rtt_ms = max(0.0, float(state.get("last_rtt") or 0.0) * 1000.0)
        except (TypeError, ValueError):
            rtt_ms = 0.0
        publish_delay_ms = 0.0
        if self.open_at is not None:
            publish_delay_ms = max(
                0.0, (self.clock.now() - float(self.open_at)) * 1000.0
            )
        state["timing_sample"] = {
            "read_rtt_ms": round(rtt_ms, 1),
            "publish_delay_ms": round(publish_delay_ms, 1),
        }

    def _t0_delta_ms(self) -> float | None:
        if self.open_at is None:
            return None
        return (self.clock.now() - float(self.open_at)) * 1000.0

    def _trace_timing(self, message: str, level: str = "info") -> None:
        delta = self._t0_delta_ms()
        stamp = "T?" if delta is None else f"T{delta:+.1f}ms"
        self.log(f"[타이밍 {stamp}] {message}", level)

    def _persist_slot_timing(self):
        state = self._live_slot_state or {}
        key = state.get("timing_key")
        sample = state.get("timing_sample")
        if key and isinstance(sample, dict):
            history = load_json(self.TIMING_HISTORY_FILE, {})
            if not isinstance(history, dict):
                history = {}
            entry = history.get(key, {})
            samples = entry.get("samples", []) if isinstance(entry, dict) else []
            samples = [item for item in samples if isinstance(item, dict)]
            samples.append(sample)
            history[key] = {"samples": samples[-self.TIMING_SAMPLE_LIMIT:]}
            try:
                save_json(self.TIMING_HISTORY_FILE, history)
            except OSError:
                pass
        slots = state.get("last_slots")
        if isinstance(slots, list) and slots:
            self._remember_slot_template(
                state.get("target_date", ""),
                state.get("zizum_num", ""),
                state.get("theme_num", ""),
                slots,
            )

    def _track_slot_background_task(self, task):
        self._slot_background_tasks.add(task)

        def finished(done):
            self._slot_background_tasks.discard(done)
            try:
                done.result()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(finished)

    async def _fetch_live_slots(self, target_date, zizum_num, theme_num, target_time):
        """Race the first boundary read; later retries stay single-requested."""
        state = self._live_slot_state or {}
        hedges_remaining = int(state.get("hedges_remaining", 0) or 0)
        if hedges_remaining <= 0:
            started = time.monotonic()
            self._trace_timing("슬롯 조회 재시도 전송")
            slots = await self._fetch_slots_with_session(
                self._session, target_date, zizum_num, theme_num
            )
            state["last_rtt"] = time.monotonic() - started
            self._trace_timing(
                f"슬롯 조회 재시도 응답 · RTT {state['last_rtt'] * 1000.0:.1f}ms"
            )
            slot_id, bookable = self._match_slot(slots, target_time)
            if (
                slot_id
                and slot_id != self.PLACEHOLDER_SLOT_ID
                and not bookable
            ):
                state["closed_observations"] = int(
                    state.get("closed_observations", 0) or 0
                ) + 1
                if int(state["closed_observations"]) < 2:
                    self._trace_timing(
                        "마감 상태 1회 감지 · 다음 독립 응답으로 확정 여부 확인"
                    )
                    return []
            return slots

        state["hedges_remaining"] = hedges_remaining - 1
        hedge_delay = float(
            state.get("hedge_delay") or self.SLOT_HEDGE_DELAY_SECONDS
        )

        async def measured_read(session, label):
            read_started = time.monotonic()
            self._trace_timing(f"슬롯 조회 {label} 전송")
            slots = await self._fetch_slots_with_session(
                session, target_date, zizum_num, theme_num
            )
            read_rtt = time.monotonic() - read_started
            self._trace_timing(
                f"슬롯 조회 {label} 응답 · RTT {read_rtt * 1000.0:.1f}ms"
            )
            return slots, read_rtt

        primary = asyncio.create_task(measured_read(self._session, "1차"))
        boundary_delay = 0.0
        if self.open_at is not None:
            boundary_delay = max(0.0, self.clock.seconds_until(self.open_at))
        # When the first read was deliberately sent early, reserve the second
        # connection for T0 itself. If the run is already late, retain the small
        # hedge stagger so both reads are not emitted in the same instant.
        timer_active = self._begin_high_resolution_timer()
        try:
            await asyncio.sleep(
                boundary_delay if boundary_delay > 0 else hedge_delay
            )
        finally:
            if timer_active:
                self._end_high_resolution_timer()

        primary_result = None
        if primary.done():
            try:
                slots, read_rtt = primary.result()
            except Exception:
                slots = []
                read_rtt = 0.0
            slot_id, bookable = self._match_slot(slots, target_time)
            if (
                slot_id
                and slot_id != self.PLACEHOLDER_SLOT_ID
                and bookable
            ):
                state["last_rtt"] = read_rtt
                return slots
            primary_result = (slots, read_rtt)

        secondary = asyncio.create_task(
            measured_read(self._get_slot_hedge_session(), "2차")
        )
        pending = {secondary}
        if primary_result is None:
            pending.add(primary)
        fallback = []
        closed_rows = []
        first_error = None
        observed_rtts = []

        def observe(slots, read_rtt):
            if read_rtt > 0:
                observed_rtts.append(read_rtt)
            slot_id, bookable = self._match_slot(slots, target_time)
            if slot_id and slot_id != self.PLACEHOLDER_SLOT_ID:
                if bookable:
                    return "ready"
                closed_rows.append(slots)
                state["closed_observations"] = int(
                    state.get("closed_observations", 0) or 0
                ) + 1
                if len(closed_rows) == 1:
                    self._trace_timing(
                        "1차 마감 상태 감지 · 2차 응답으로 확정 여부 확인"
                    )
                return "closed"
            if slots and not fallback:
                fallback.extend(slots)
            return "pending"

        if primary_result is not None:
            observe(*primary_result)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    slots, read_rtt = task.result()
                except Exception as exc:
                    first_error = first_error or exc
                    continue
                if observe(slots, read_rtt) == "ready":
                    state["last_rtt"] = read_rtt
                    for remaining in pending:
                        self._track_slot_background_task(remaining)
                    return slots
        if observed_rtts:
            state["last_rtt"] = min(observed_rtts)
        if (
            closed_rows
            and int(state.get("closed_observations", 0) or 0) >= 2
        ):
            return closed_rows[-1]
        if fallback:
            return fallback
        if closed_rows:
            return []
        if first_error is not None:
            raise first_error
        return []

    async def _fetch_coordinated_live_slots(
        self, target_date, zizum_num, theme_num, target_time
    ):
        """Share one public timetable response across local Pengucro processes."""
        share = self._slot_share
        if share is None:
            return await self._fetch_live_slots(
                target_date, zizum_num, theme_num, target_time
            )
        if share.owner:
            share.mark_started()
            slots = await self._fetch_live_slots(
                target_date, zizum_num, theme_num, target_time
            )
            if slots:
                async def publish_shared_rows():
                    try:
                        await asyncio.to_thread(share.publish, slots)
                    except OSError:
                        pass

                self._track_slot_background_task(
                    asyncio.create_task(publish_shared_rows())
                )
            return slots

        wait_started = time.monotonic()
        slots = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: share.wait_for_result(self.SHARED_SLOT_WAIT_SECONDS),
        )
        waited = time.monotonic() - wait_started
        if slots:
            state = self._live_slot_state or {}
            state["last_rtt"] = waited
            self._trace_timing(
                f"다른 실행의 동일 시간표 응답 수신 · 공유 대기 {waited * 1000.0:.1f}ms"
            )
            return slots

        self._trace_timing(
            "공유 시간표 응답이 없어 이 실행의 독립 조회로 전환",
            "warning",
        )
        # Do not pay the rendezvous timeout again on later retries.
        self._slot_share = None
        return await self._fetch_live_slots(
            target_date, zizum_num, theme_num, target_time
        )

    async def _prewarm_slot_connections(self):
        """Warm the two read-only connections without downloading a page body."""
        url = f"{self.site_url}/reservation.php"
        sessions = (self._session, self._get_slot_hedge_session())
        loop = asyncio.get_running_loop()
        results = await asyncio.gather(*(
            loop.run_in_executor(None, lambda active=session: active.head(url, timeout=5.0))
            for session in sessions
        ), return_exceptions=True)
        return sum(not isinstance(result, Exception) for result in results)

    async def _prewarm_browser_connection(self, page) -> bool:
        if page is None:
            return False
        try:
            result = await page.evaluate(
                """async () => {
                    try {
                        const response = await fetch(
                            '/reservation.php?pg_prewarm=' + Date.now(),
                            {
                                method: 'HEAD',
                                cache: 'no-store',
                                credentials: 'include',
                            }
                        );
                        return response.ok;
                    } catch (e) {
                        return false;
                    }
                }"""
            )
            return bool(result)
        except Exception:
            return False

    async def _prewarm_near_open(self, page=None):
        state = self._live_slot_state or {}
        if state.get("status") in ("ready", "capacity"):
            return
        if self.open_at is not None:
            while not self.stop_event.is_set():
                remaining = self.clock.seconds_until(self.open_at)
                if remaining <= 0:
                    return
                if remaining <= self.SLOT_PREWARM_LEAD_SECONDS:
                    break
                await asyncio.sleep(min(1.0, max(0.05, remaining - self.SLOT_PREWARM_LEAD_SECONDS)))
        if self.stop_event.is_set():
            return
        warmed, browser_warmed = await asyncio.gather(
            self._prewarm_slot_connections(),
            self._prewarm_browser_connection(page),
        )
        self.log(
            f"[정보] 오픈 경계용 슬롯 조회 연결 {warmed}/2개 예열 완료",
            "info" if warmed == 2 else "warning",
        )
        self.log(
            "[정보] 예약 제출용 Chrome 연결 예열 "
            f"{'완료' if browser_warmed else '실패 · 기존 연결로 계속 진행'}",
            "info" if browser_warmed else "warning",
        )
        if self._slot_share is not None:
            self.log(
                "[정보] 동일 지점·테마·날짜 시간표 "
                + (
                    "대표 조회 실행으로 준비했습니다."
                    if self._slot_share.owner
                    else "다른 실행의 대표 조회 결과를 공유받습니다."
                ),
                "info",
            )

    # NOTE: the endDay flag is sent as the site sends it, but it was measured to
    # make no difference at all -- responses for endDay=0 and endDay=1 were
    # byte-identical across today, the last selectable date, and dates beyond the
    # window. There is therefore nothing to probe or fall back to.

    def _report_slot_change(self, slots, target_time):
        """Log the raw state of the target row whenever it changes.

        Gives a record of the actual N -> Y transition, which is the evidence
        needed to confirm that this really is the right trigger.
        """
        row = next(
            (s for s in slots if self._slot_time(s) == target_time), None
        )
        state = (
            "없음" if row is None
            else f"num={row.get('num')} enable={row.get('enable')} "
                 f"sale={row.get('sale_txt') or '-'}"
        )
        if state != self._last_slot_state:
            self._last_slot_state = state
            self.log(f"[정보] {target_time} 슬롯 상태 변화 · {state}", "info")

    @staticmethod
    def _slot_time(slot):
        try:
            return f"{int(slot.get('hh', 0)):02d}:{int(slot.get('mm', 0)):02d}"
        except (TypeError, ValueError):
            return ""

    @classmethod
    def _match_slot(cls, slots, target_time):
        """Return (slot_id, bookable) for the requested time.

        ``enable`` is the site's own gate: its front-end renders 'N' as a
        disabled option. Treating a disabled row as open is what previously
        caused premature submissions.
        """
        for slot in slots:
            if cls._slot_time(slot) != target_time:
                continue
            slot_id = str(slot.get("num", "") or "")
            bookable = str(slot.get("enable", "")).upper() == "Y" and bool(slot_id)
            return slot_id, bookable
        return "", False

    async def _resolve_live_slot(
        self, target_date, target_time, zizum_num, theme_num, current_slot=""
    ):
        """Return the target date's real slot id once the server publishes it.

        All standby pages share one state and one lock, so speed does not require
        three duplicate requests at the opening boundary.  A caller without the
        shared production state (small unit tests/legacy callers) may keep an
        already-known non-placeholder target slot.
        """
        state = getattr(self, "_live_slot_state", None)
        if state is None:
            if current_slot and current_slot != self.PLACEHOLDER_SLOT_ID:
                return str(current_slot), "ready"
            state = {
                "slot_id": "", "status": "pending", "last_probe": 0.0,
                "lock": asyncio.Lock(),
            }
            self._live_slot_state = state

        if state.get("status") in ("ready", "capacity"):
            return str(state.get("slot_id") or ""), str(state.get("status"))

        async with state["lock"]:
            if state.get("status") in ("ready", "capacity"):
                return str(state.get("slot_id") or ""), str(state.get("status"))
            now = time.monotonic()
            retry_delay = float(
                state.get("retry_delay") or self.SLOT_OPEN_RETRY_SECONDS
            )
            if now - float(state.get("last_probe") or 0.0) < retry_delay:
                return "", "pending"
            state["last_probe"] = now
            try:
                fetcher = state.get("fetcher") or self._fetch_slots
                if state.get("fetcher_accepts_target"):
                    slots = await fetcher(
                        target_date, zizum_num, theme_num, target_time
                    )
                else:
                    slots = await fetcher(target_date, zizum_num, theme_num)
            except Exception as exc:
                self._log_throttled(
                    "live_slot_error", f"[경고] 오픈 슬롯 확인 실패: {exc}",
                    "warning", interval=2.0,
                )
                return "", "pending"

            try:
                measured_rtt = float(state.get("last_rtt") or 0.0)
            except (TypeError, ValueError):
                measured_rtt = 0.0
            if measured_rtt > 0:
                state["retry_delay"] = min(
                    self.SLOT_RETRY_MAX_SECONDS,
                    max(self.SLOT_RETRY_MIN_SECONDS, measured_rtt),
                )
            slot_id, bookable = self._match_slot(slots, target_time)
            if not slot_id or slot_id == self.PLACEHOLDER_SLOT_ID:
                return "", "pending"
            state["slot_id"] = slot_id
            state["status"] = "ready" if bookable else "capacity"
            state["last_slots"] = slots
            self._trace_timing(
                f"실제 슬롯 확인 · ID {slot_id} · "
                f"{'예약 가능' if bookable else '이미 마감'}"
            )
            remember = state.get("record_timing")
            if remember is not None:
                remember(state)
            return slot_id, str(state["status"])

    async def _fetch_window_info(self, zizum_num, theme_info_num):
        """Return (doing_days, open_time) read live from the site.

        ``doing`` is how many days ahead the window reaches; the open time is
        stated in the branch notice (``zizum.doc_1``) or, failing that, the theme
        memo. Reading it costs nothing and is more trustworthy than the bundled
        table -- 무비무드 전주 is listed there as 13:30 while the site says 18:00.
        """
        try:
            data = await self._post_async({
                't': 'get_theme_info_list',
                'zizum_num': zizum_num,
            })
        except Exception:
            return 0, None
        if not data.get("status"):
            return 0, None

        doing = 0
        theme_memo = ""
        for theme in data.get("data") or []:
            if str(theme.get("info_num")) == str(theme_info_num):
                try:
                    doing = int(theme.get("doing", 0))
                except (TypeError, ValueError):
                    doing = 0
                theme_memo = theme.get("memo") or ""
                break

        branch = data.get("zizum") or {}
        for field in ("doc_1", "soge", "doc_2", "doc_3"):
            found = parse_open_time(branch.get(field))
            if found:
                return doing, found
        found = parse_open_time(theme_memo)
        return doing, found

    # ------------------------------------------------------------------
    # Theme resolution
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_theme(reservation_data, zizum_num, theme_info_num):
        from data.themes import KEYESCAPE_THEMES

        metadata = reservation_data.get("engine_metadata", {})
        theme_meta = metadata.get("theme", {}) if isinstance(metadata, dict) else {}
        theme_num = str(theme_meta.get("theme_num", "") or "")
        theme_name = reservation_data.get("themeLabel", "")

        for branch_id, themes in KEYESCAPE_THEMES.items():
            if branch_id != zizum_num:
                continue
            for name, ids in themes.items():
                if isinstance(ids, dict) and str(ids.get("info_num")) == str(theme_info_num):
                    theme_num = str(ids.get("theme_num", "") or theme_num)
                    theme_name = name
                    break
            break

        if not theme_num:
            theme_num = str(theme_info_num)
            theme_name = theme_name or "테마"
        return theme_num, theme_name

    def _set_shared_open_at(self, epoch):
        """Apply a server-corrected opening moment to every standby page."""
        self.open_at = epoch
        for worker in self._page_workers:
            worker.open_at = epoch

    def _make_page_worker(self, page_index):
        """Create isolated captcha/page state while sharing the run coordinator."""
        worker = KeyescapeEngine(
            log_callback=self.log_callback,
            success_callback=None,
            site_url=self.site_url,
        )
        worker.stop_event = self.stop_event
        worker.listener_stop = self.listener_stop
        worker.clock = self.clock
        worker._clock_share = self._clock_share
        worker.open_at = self.open_at
        worker._page_index = page_index
        worker._page_count = self._page_count
        worker._page_success_event = self._page_success_event
        worker._live_slot_state = self._live_slot_state
        worker._trusted_slot_id = (
            self._trusted_slot_id if page_index == 1 else ""
        )
        worker._trusted_slot_sources = self._trusted_slot_sources
        worker._clock_sync_enabled = page_index == 1
        worker._open_at_update_callback = self._set_shared_open_at
        prefix = f"[{page_index}번 페이지]"

        worker.log = lambda message, log_type="info": self.log(
            f"{prefix} {message}", log_type
        )
        worker.silent_tick = lambda message: self.silent_tick(
            f"{prefix} {message}"
        )

        def notify(result=None):
            won = self.notify_success(result)
            self._page_success_event.set()
            if won:
                self._winner_page = page_index
            return won

        worker.notify_success = notify
        return worker

    async def _prepare_standby_page(
        self,
        context,
        page_index,
        reservation_data,
        zizum_num,
        theme_num,
        theme_info_num,
        target_date,
        form_slot_id,
        target_time,
        theme_name,
    ):
        worker = self._make_page_worker(page_index)
        page = None
        try:
            page = await context.new_page()
            page.set_default_timeout(15000)
            try:
                await page.add_init_script(DEVTOOLS_GUARD_SCRIPT)
            except Exception:
                pass
            dialog_state = self._new_submission_state()
            await worker._prepare_page(page, dialog_state)
            await worker._enter_step_two(
                page,
                zizum_num,
                theme_num,
                theme_info_num,
                target_date,
                form_slot_id,
                target_time,
                theme_name,
            )
            await worker._fill_form(page, reservation_data)
            worker.log("예약 페이지 준비 완료 · 독립 캡차 토큰 대기", "success")
            return worker, page, dialog_state
        except Exception as exc:
            worker.log(f"[경고] 페이지 준비 실패: {exc}", "warning")
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            try:
                worker._session.close()
            except Exception:
                pass
            return None

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------
    async def _run_browser_booking_async(self, reservation_data):
        from playwright.async_api import async_playwright

        target_date = reservation_data['reservationDate']
        target_time = reservation_data['reservationTime'][:5]
        zizum_num = str(reservation_data['branch'])
        theme_info_num = str(reservation_data['themePK'])
        theme_num, theme_name = self._resolve_theme(
            reservation_data, zizum_num, theme_info_num
        )

        self.log(
            f"키이스케이프 예약 준비 · {theme_name} · {target_date} {target_time}",
            "info",
        )

        # --- the site's clock, not this machine's -------------------------
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._sync_server_clock(announce=True)
        )

        # --- when does the window open -----------------------------------
        doing_days, notice_open_time = await self._fetch_window_info(
            zizum_num, theme_info_num
        )
        self.open_at = self._resolve_open_moment(
            zizum_num, target_date, doing_days, notice_open_time
        )

        # --- slot id -----------------------------------------------------
        # A pre-open row is useful only for building Step 2. The final request
        # always replaces it with the target date's post-open id.
        slot_id = ""
        slot_bookable = False
        initial_slot_rtt = 0.0
        try:
            initial_slot_started = time.monotonic()
            slots = await self._fetch_slots(target_date, zizum_num, theme_num)
            initial_slot_rtt = time.monotonic() - initial_slot_started
            if slots:
                self._remember_slot_template(
                    target_date, zizum_num, theme_num, slots
                )
            slot_id, slot_bookable = self._match_slot(slots, target_time)
            if not slot_id:
                for row in slots:
                    if self._slot_time(row) == target_time:
                        slot_id = str(row.get("num", "") or "")
                        break
            self._report_slot_change(slots, target_time)
            if slot_id:
                self.log(f"[정보] {target_time} 슬롯 ID {slot_id} 확인", "info")
            else:
                available = ", ".join(sorted({self._slot_time(s) for s in slots if self._slot_time(s)}))
                self.log(
                    f"[경고] {target_time} 시간대를 찾지 못했습니다. "
                    f"조회된 시간: {available or '없음'}",
                    "warning",
                )
        except Exception as exc:
            self.log(f"[경고] 시간 조회 실패: {exc}", "warning")

        self._trusted_slot_id, self._trusted_slot_sources = (
            await self._prime_trusted_slot_template(
                target_date,
                target_time,
                zizum_num,
                theme_num,
                doing_days,
            )
        )
        if self._trusted_slot_id:
            basis = (
                "동일 시간표 2개 날짜 일치"
                if len(self._trusted_slot_sources) >= 2
                else "사이트 요일 시간표 그룹 일치"
            )
            self.log(
                "[정보] 검증 시간표 빠른 제출 준비 · "
                f"슬롯 ID {self._trusted_slot_id} · 기준 날짜 "
                f"{', '.join(self._trusted_slot_sources)} · {basis} · "
                "1번 페이지만 오픈 직후 선발사하고 나머지는 실시간 조회를 유지합니다.",
                "success",
            )
        else:
            self.log(
                "[정보] 빠른 제출 시간표가 안전 기준을 충족하지 않아 모든 페이지를 "
                "기존 실시간 슬롯 조회 방식으로 유지합니다.",
                "info",
            )

        form_slot_id = slot_id or self.PLACEHOLDER_SLOT_ID
        already_open = (
            self.open_at is None or self.clock.seconds_until(self.open_at) <= 0
        )
        self._ensure_parallel_live_fallback(already_open)
        verified_slot = bool(slot_id and already_open)
        timing_key, hedge_delay, retry_delay, read_lead, has_timing = (
            self._load_timing_profile(
                zizum_num, theme_num, target_time
            )
        )
        if not has_timing and initial_slot_rtt > 0:
            hedge_delay, retry_delay, read_lead = (
                self._cold_start_timing_parameters(initial_slot_rtt)
            )
        self._live_slot_state = {
            "slot_id": slot_id if verified_slot else "",
            "status": (
                "ready" if verified_slot and slot_bookable else
                "capacity" if verified_slot else "pending"
            ),
            "last_probe": 0.0,
            "lock": asyncio.Lock(),
            "fetcher": self._fetch_coordinated_live_slots,
            "fetcher_accepts_target": True,
            "hedges_remaining": 1,
            "hedge_delay": hedge_delay,
            "retry_delay": retry_delay,
            "read_lead": read_lead,
            "last_rtt": 0.0,
            "closed_observations": 0,
            "timing_key": timing_key,
            "timing_sample": None,
            "record_timing": self._remember_slot_timing,
            "target_date": target_date,
            "zizum_num": zizum_num,
            "theme_num": theme_num,
        }
        if self.open_at is not None:
            share_key = (
                f"{self.site_url.lower()}|{zizum_num}|{theme_num}|{target_date}"
            )
            self._slot_share = SharedSlotLookup(share_key, self.open_at)
            self._slot_share.prepare()

        async with async_playwright() as playwright:
            browser, context, chrome_session, owns_browser = await self._open_browser(playwright)
            if browser is None or context is None:
                return
            try:
                # Must happen before the first page exists: init scripts only
                # apply to documents created after registration.
                await self._harden_context(context)
                prepared = await asyncio.gather(*(
                    self._prepare_standby_page(
                        context,
                        page_index,
                        reservation_data,
                        zizum_num,
                        theme_num,
                        theme_info_num,
                        target_date,
                        (
                            self._trusted_slot_id
                            if page_index == 1 and self._trusted_slot_id
                            else form_slot_id
                        ),
                        target_time,
                        theme_name,
                    )
                    for page_index in range(1, self._page_count + 1)
                ))
                prepared = [entry for entry in prepared if entry is not None]
                if not prepared:
                    self.log("[에러] 준비된 키이스케이프 예약 페이지가 없습니다.", "error")
                    return
                self._page_workers = [entry[0] for entry in prepared]
                self.log(
                    f"[정보] 핫 스탠바이 {len(prepared)}개 페이지 준비 완료 · "
                    "각 페이지는 독립 YesCaptcha 토큰을 사용합니다.",
                    "success",
                )
                prewarm_task = asyncio.create_task(
                    self._prewarm_near_open(prepared[0][1])
                )
                results = await asyncio.gather(*(
                    worker._watch_and_submit(
                        page,
                        dialog_state,
                        reservation_data,
                        target_date,
                        target_time,
                        zizum_num,
                        theme_num,
                        theme_name,
                        slot_id,
                    )
                    for worker, page, dialog_state in prepared
                ), return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception) and not self.stop_event.is_set():
                        self.log(f"[경고] 스탠바이 페이지 작업 오류: {result}", "warning")
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.log(f"[에러] 키이스케이프 처리 중 오류: {exc}", "error")
            finally:
                prewarm_task = locals().get("prewarm_task")
                if prewarm_task is not None:
                    if not prewarm_task.done():
                        prewarm_task.cancel()
                    try:
                        await prewarm_task
                    except (asyncio.CancelledError, Exception):
                        pass
                for worker in self._page_workers:
                    await worker._cancel_yescaptcha_task()
                    try:
                        worker._session.close()
                    except Exception:
                        pass
                # Keep only the winning page (or page 1 on manual stop) for
                # review; close standby tabs so repeated runs do not leak tabs.
                keep_page = self._winner_page or 1
                for worker, page, _dialog_state in locals().get("prepared", []):
                    page_index = worker._page_index
                    if page_index == keep_page:
                        continue
                    try:
                        await page.close()
                    except Exception:
                        pass
                self._page_workers = []
                # The worker tears its own browser down. Doing it from another
                # thread raced with whatever this coroutine was mid-way through.
                if owns_browser:
                    for closer in (context, browser):
                        try:
                            if closer is not None:
                                await closer.close()
                        except Exception:
                            continue
                # For CDP-attached Chrome, do not call browser.close(). Multiple
                # Pengucro processes may each own an independent driver connection;
                # ending one explicitly can invalidate its persistent default
                # context while another operation is still settling. Exiting the
                # async_playwright block below disposes only this Playwright driver.
                if self._slot_background_tasks:
                    try:
                        await asyncio.wait(
                            tuple(self._slot_background_tasks), timeout=5.0
                        )
                    except Exception:
                        pass
                try:
                    self._session.close()
                except Exception:
                    pass
                if self._slot_hedge_session is not None:
                    try:
                        self._slot_hedge_session.close()
                    except Exception:
                        pass
                self._persist_slot_timing()
                if chrome_session is not None and self._close_chrome_on_exit:
                    chrome_session.close_if_launched()
                if chrome_session is not None:
                    chrome_session.release()

    def _resolve_open_moment(self, zizum_num, target_date, doing_days, notice_open_time):
        """Epoch seconds (server clock) at which the window opens, or None.

        Preference: the time stated in the branch notice, then the bundled table.
        A rejection from the booking endpoint later overrides both, because that
        message carries the server's own answer.
        """
        try:
            target = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            self.log("[경고] 날짜 형식을 해석하지 못했습니다.", "warning")
            return None

        if notice_open_time:
            hour, minute = notice_open_time
            source = "사이트 안내문"
        else:
            table = BRANCH_OPEN_TIMES.get(str(zizum_num))
            if not table:
                self.log(
                    "[경고] 오픈 시각을 알 수 없습니다. 캡차 인증 후 즉시 1회 제출하고, "
                    "서버 응답으로 오픈 시각을 다시 계산합니다.",
                    "warning",
                )
                return None
            hour, minute = table
            source = "내장 표(안내문 파싱 실패)"

        if doing_days <= 0:
            self.log("[경고] 예약 가능 기간(doing)을 읽지 못해 오픈일을 계산할 수 없습니다.", "warning")
            return None

        open_date = target - timedelta(days=doing_days - 1)
        open_at = datetime(
            open_date.year, open_date.month, open_date.day, hour, minute, tzinfo=KST
        )
        epoch = open_at.timestamp()
        self._announce_open(epoch, f"{open_date} {hour:02d}:{minute:02d}", source)
        return epoch

    def _announce_open(self, epoch, stamp, source):
        remaining = self.clock.seconds_until(epoch)
        if remaining <= 0:
            self.log(f"[정보] 오픈 예정 {stamp} ({source}) · 이미 지났습니다.", "info")
            return
        self.log(
            f"[정보] 오픈 예정 {stamp} ({source}) · 서버 시간 기준 "
            f"{self._format_remaining(remaining)} 남음",
            "info",
        )

    @staticmethod
    def _format_remaining(seconds):
        seconds = max(0, int(seconds))
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes, secs = divmod(rest, 60)
        if days:
            return f"{days}일 {hours}시간 {minutes}분"
        if hours:
            return f"{hours}시간 {minutes}분 {secs}초"
        if minutes:
            return f"{minutes}분 {secs}초"
        return f"{secs}초"

    @staticmethod
    def _begin_high_resolution_timer():
        try:
            return ctypes.windll.winmm.timeBeginPeriod(1) == 0
        except (AttributeError, OSError):
            return False

    @staticmethod
    def _end_high_resolution_timer():
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except (AttributeError, OSError):
            pass

    def _live_slot_read_lead(self):
        state = self._live_slot_state or {}
        try:
            lead = float(state.get("read_lead") or self.SLOT_READ_LEAD_SECONDS)
        except (TypeError, ValueError):
            lead = self.SLOT_READ_LEAD_SECONDS
        return min(
            self.SLOT_READ_LEAD_MAX_SECONDS,
            max(self.SLOT_READ_LEAD_MIN_SECONDS, lead),
        )

    async def _wait_for_open_quiet(self):
        """Wait until the read-send point without Playwright or UI work."""
        timer_active = self._begin_high_resolution_timer()
        try:
            read_lead = self._live_slot_read_lead()
            while not self.stop_event.is_set() and self.open_at is not None:
                remaining = self.clock.seconds_until(self.open_at)
                if remaining <= read_lead:
                    return
                await asyncio.sleep(0.02 if remaining - read_lead > 0.25 else 0.001)
        finally:
            if timer_active:
                self._end_high_resolution_timer()

    async def _wait_for_trusted_fire(self):
        """Never let the verified-template page cross the server gate early."""
        target = self._trusted_fire_server_epoch()
        if target is None:
            return

        timer_active = self._begin_high_resolution_timer()
        try:
            while not self.stop_event.is_set():
                remaining = target - self.clock.now()
                if remaining <= 0:
                    return
                await asyncio.sleep(0.001 if remaining <= 0.2 else min(0.02, remaining / 2))
        finally:
            if timer_active:
                self._end_high_resolution_timer()

    def _trusted_fire_server_epoch(self):
        if self.open_at is None:
            return None
        try:
            precision = float(self.clock.last_precision or 0.06)
        except (TypeError, ValueError):
            precision = 0.06
        safety = min(0.12, max(0.02, precision) + self.TRUSTED_FIRE_EXTRA_SECONDS)
        return float(self.open_at) + safety

    def _trusted_fire_client_epoch_ms(self):
        target = self._trusted_fire_server_epoch()
        if target is None:
            return 0.0
        return (time.time() + max(0.0, target - self.clock.now())) * 1000.0

    def _live_slot_retry_delay(self):
        state = self._live_slot_state or {}
        try:
            delay = float(state.get("retry_delay") or self.SLOT_OPEN_RETRY_SECONDS)
        except (TypeError, ValueError):
            delay = self.SLOT_OPEN_RETRY_SECONDS
        return min(self.SLOT_RETRY_MAX_SECONDS, max(self.SLOT_RETRY_MIN_SECONDS, delay))

    # ------------------------------------------------------------------
    # Browser setup
    # ------------------------------------------------------------------
    async def _open_browser(self, playwright):
        """Return (browser, context, chrome_session, owns_browser).

        Preference order:
          1. A real Chrome reached over the DevTools protocol, using a dedicated
             persistent profile. Its cookies, history and Google session survive
             between runs, which is what stops reCAPTCHA from treating every
             launch as an unknown first-time visitor.
          2. Playwright's own managed browser with a throwaway profile. Works,
             but scores badly and will usually show the image challenge.
        """
        if self._use_real_chrome:
            session = browser_session.start_isolated(log=self.log)
            if session is not None:
                try:
                    browser = await playwright.chromium.connect_over_cdp(session.endpoint)
                    # contexts[0] is Chrome's persistent profile context. Calling
                    # new_context() here would defeat the whole purpose by
                    # creating an empty incognito profile again.
                    context = browser.contexts[0] if browser.contexts else await browser.new_context()
                    self.log(
                        "실제 Chrome 프로필에 연결했습니다. "
                        f"(프로필: {session.profile_path})",
                        "success",
                    )
                    return browser, context, session, False
                except Exception as exc:
                    self.log(f"[경고] Chrome DevTools 연결 실패: {exc}", "warning")
                    session.close_if_launched()
                    session.release()

            self.log(
                "[경고] 실제 Chrome 연결에 실패해 임시 프로필로 진행합니다. "
                "이 경우 자동등록방지 퍼즐이 나올 가능성이 높습니다.",
                "warning",
            )

        attempts = [
            {"channel": "chrome", "label": "Google Chrome"},
            {"channel": "msedge", "label": "Microsoft Edge"},
            {"channel": None, "label": "Chromium"},
        ]
        errors = []
        for attempt in attempts:
            if self.stop_event.is_set():
                return None, None, None, True
            kwargs = {"headless": False, "args": ["--disable-infobars"]}
            if attempt["channel"]:
                kwargs["channel"] = attempt["channel"]
            try:
                browser = await playwright.chromium.launch(**kwargs)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    locale="ko-KR",
                )
                self.log(f"{attempt['label']} 브라우저를 실행했습니다.", "success")
                return browser, context, None, True
            except Exception as exc:
                errors.append(f"{attempt['label']}: {exc}")
        self.log(f"[에러] 브라우저 실행 실패 · {' / '.join(errors)}", "error")
        return None, None, None, True

    async def _harden_context(self, context):
        """Disarm the site's devtools guard for every page in this context.

        Registered on the context, not a page, so popups and any tab the site
        opens itself inherit it. Both layers are cheap and independent:
        the init script wins even on a cache hit, the route stops the real
        library from ever being parsed when the request does reach the network.
        """
        try:
            await context.add_init_script(DEVTOOLS_GUARD_SCRIPT)
        except Exception as exc:
            self.log(f"[경고] 개발자도구 감지 우회 스크립트 등록 실패: {exc}", "warning")

        async def stub_detector(route):
            try:
                await route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body="/* devtools-detector disabled */",
                )
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        try:
            await context.route(re.compile(r"devtools-detector"), stub_detector)
        except Exception as exc:
            self.log(f"[경고] 개발자도구 감지 스크립트 차단 실패: {exc}", "warning")

    @staticmethod
    def _new_submission_state():
        return {
            "message": "",
            "submission_status": "",
            "booking_number": "",
            "ck_code": "",
            "request_started": False,
            "request_finished": False,
            "request_failed": False,
            "request_failure": "",
        }

    @staticmethod
    def _dialog_indicates_success(message):
        compact = re.sub(r"\s+", "", message or "")
        if any(token in compact for token in ("예약불가", "이미완료", "이미예약", "마감")):
            return False
        return any(token in compact for token in (
            "입금확인이되면예약확정",
            "예약신청이완료",
            "예약이완료되었습니다",
        ))

    @staticmethod
    def _record_submission_payload(dialog_state, payload):
        """Record the booking endpoint's JSON before the page can navigate away."""
        if not isinstance(payload, dict):
            return False
        message = str(payload.get("msg", "") or "")
        if message:
            dialog_state["message"] = message
        if not payload.get("status"):
            if message:
                dialog_state["submission_status"] = "failure"
            return False

        details = payload.get("data")
        details = details if isinstance(details, dict) else {}
        dialog_state["submission_status"] = "success"
        dialog_state["booking_number"] = str(details.get("num", "") or "")
        dialog_state["ck_code"] = str(details.get("ck_code", "") or "")
        return True

    async def _prepare_page(self, page, dialog_state):
        async def handle_dialog(dialog):
            dialog_state["message"] = dialog.message or ""
            if self._dialog_indicates_success(dialog_state["message"]):
                dialog_state["submission_status"] = "success"
                self._page_success_event.set()
                self.log(
                    f"[서버 응답] 예약 접수 성공 · {dialog_state['message'][:120]}",
                    "success",
                )
            else:
                dialog_state["submission_status"] = "failure"
                if (
                    self._page_count > 1
                    and self._classify_failure(dialog_state["message"]) == "capacity"
                ):
                    self.log(
                        "[핫 스탠바이 응답] 이 페이지는 이미 예약 완료 응답을 받았습니다. "
                        "다른 페이지의 성공 여부를 확인합니다.",
                        "info",
                    )
                else:
                    self.log(
                        f"[알림] 사이트 메시지: {dialog_state['message'][:120]}",
                        "warning",
                    )
            try:
                await dialog.accept()
            except Exception:
                pass

        async def handle_response(response):
            try:
                if "/controller/run_proc.php" not in (response.url or ""):
                    return
                request = response.request
                if str(getattr(request, "method", "") or "").upper() != "POST":
                    return
                post_data = getattr(request, "post_data", "") or ""
                if callable(post_data):
                    post_data = post_data()
                payload = await response.json()
                details = payload.get("data") if isinstance(payload, dict) else None
                has_booking_number = (
                    isinstance(details, dict)
                    and bool(details.get("num"))
                )
                success_payload = bool(
                    isinstance(payload, dict)
                    and payload.get("status")
                    and (
                        has_booking_number
                        or self._dialog_indicates_success(payload.get("msg", ""))
                    )
                )
                # The endpoint serves several actions. Prefer the submitted
                # action marker; Chromium can omit multipart post_data, and newer
                # responses do not always include ck_code, so a success message or
                # reservation number is also sufficient evidence.
                if "ins_rev" not in str(post_data) and not success_payload:
                    return
                self._trace_timing("최종 예약 POST 응답 수신")
                if self._record_submission_payload(dialog_state, payload):
                    self._page_success_event.set()
            except Exception:
                # Dialog and completion-page detection remain as fallbacks.
                return

        async def handle_request(request):
            try:
                if "/controller/run_proc.php" not in (request.url or ""):
                    return
                if str(getattr(request, "method", "") or "").upper() != "POST":
                    return
                post_data = getattr(request, "post_data", "") or ""
                if callable(post_data):
                    post_data = post_data()
                if "ins_rev" in str(post_data):
                    self._trace_timing("최종 예약 POST 네트워크 전송")
            except Exception:
                return

        page.on("dialog", handle_dialog)
        page.on("request", handle_request)
        page.on("response", handle_response)

    async def _is_blocked(self, page):
        """True when the site has replaced the page with its block screen."""
        try:
            return bool(await page.evaluate(
                "(marker) => (document.body ? document.body.innerText : '').includes(marker)",
                DEVTOOLS_BLOCK_MARKER,
            ))
        except Exception:
            return False

    async def _enter_step_two(
        self, page, zizum_num, theme_num, theme_info_num,
        target_date, slot_id, target_time, theme_name,
    ):
        def esc(value):
            return str(value).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")

        fields = {
            "zizumNum": zizum_num,
            "themeNum": theme_num,
            "themeInfoNum": theme_info_num,
            "revDays": target_date,
            "themeTimeNum": slot_id,
            "revTimes": target_time,
            "themeName": theme_name,
        }
        inputs = "".join(
            f'<input type="hidden" name="{name}" value="{esc(value)}">'
            for name, value in fields.items()
        )
        html = (
            "<html><body><form id='f' method='POST' "
            f"action='{self.reservation_url}'>{inputs}</form>"
            "<script>document.getElementById('f').submit();</script></body></html>"
        )
        self.log("예약 정보 입력 화면으로 이동합니다.", "info")
        self._step_two_html = html
        await page.set_content(html)
        await page.wait_for_load_state("domcontentloaded")

    async def _restore_step_two(self, page, reservation_data):
        """Rebuild the step 2 screen after the site wiped it.

        The guard script should stop this from ever being needed; it exists so a
        wiped page cannot strand the run in a loop that polls a dead document.
        """
        if not getattr(self, "_step_two_html", ""):
            return False
        try:
            await page.set_content(self._step_two_html)
            await page.wait_for_load_state("domcontentloaded")
            await self._fill_form(page, reservation_data)
            return True
        except Exception as exc:
            self.log(f"[경고] 예약 화면 복구 실패: {exc}", "warning")
            return False

    async def _fill_form(self, page, reservation_data):
        filled = []
        people = str(reservation_data.get("people", "2"))
        try:
            await page.locator("select#person").select_option(value=people)
            filled.append(f"인원 {people}")
        except Exception:
            pass

        try:
            await page.locator("input#name_input").fill(str(reservation_data.get("name", "")))
            filled.append("예약자명")
        except Exception:
            pass

        digits = "".join(c for c in str(reservation_data.get("phone", "")) if c.isdigit())
        if len(digits) == 11:
            mid, tail = digits[3:7], digits[7:11]
        elif len(digits) == 10:
            mid, tail = digits[3:6], digits[6:10]
        else:
            mid = tail = ""
        if mid and tail:
            try:
                await page.locator("input[name=mobile2]").fill(mid)
                await page.locator("input[name=mobile3]").fill(tail)
                filled.append("연락처")
            except Exception:
                pass

        # Consents.
        #
        # The site's own handler only ticks 전체동의 once *every* .agree_btn is
        # checked, and that class covers the optional marketing box too. Ticking
        # just the two mandatory ones therefore left 전체동의 visibly unchecked.
        # So 전체동의 is driven directly and the site's cascade fills the rest,
        # which is exactly what a person clicking 전체동의 would produce.
        #
        # Submission itself only requires agree_1 and agree_2, so the marketing
        # opt-in can be skipped with "keyescape_agree_all": false in config.json.
        try:
            checked = await page.evaluate(
                """(agreeAll) => {
                    const fire = (box) => {
                        box.checked = true;
                        box.dispatchEvent(new Event('change', { bubbles: true }));
                    };
                    if (agreeAll) {
                        const all = document.getElementById('agree_all');
                        if (all) {
                            fire(all);
                            // The page listens on #agree_all and mirrors it onto
                            // every .agree_btn; do it here as well in case the
                            // handler is not bound yet.
                            document.querySelectorAll('.agree_btn').forEach((box) => {
                                if (!box.checked) { fire(box); }
                            });
                        }
                    }
                    for (const name of ['agree_1', 'agree_2']) {
                        const box = document.querySelector(`input[name="${name}"]`);
                        if (box && !box.checked) { fire(box); }
                    }
                    return Array.from(
                        document.querySelectorAll('.resrv_agree input[type=checkbox]')
                    ).filter((box) => box.checked).length;
                }""",
                self._agree_all,
            )
            filled.append(
                f"{'전체' if self._agree_all else '필수'} 약관 동의({checked}개)"
            )
        except Exception as exc:
            self.log(f"[경고] 약관 동의 자동 체크 실패: {exc}", "warning")

        if filled:
            self.log(f"입력 완료 · {' · '.join(filled)}", "info")
        else:
            self.log(
                "[경고] 예약 양식을 자동으로 채우지 못했습니다. 브라우저에서 직접 입력해주세요.",
                "warning",
            )

    # ------------------------------------------------------------------
    # reCAPTCHA
    # ------------------------------------------------------------------
    async def _read_sitekey(self, page):
        """The site key rendered on the live page, falling back to the known one.

        Hard-coding it meant a silent, unexplainable failure the day the site
        rotates the key: YesCaptcha happily solves the *wrong* widget and the
        booking endpoint rejects the token as '잘못된 접근'.
        """
        if self._sitekey:
            return self._sitekey
        try:
            found = await page.evaluate(
                """() => {
                    const el = document.querySelector('#captcha[data-sitekey]')
                        || document.querySelector('.g-recaptcha[data-sitekey]')
                        || document.querySelector('[data-sitekey]');
                    if (el) { return el.getAttribute('data-sitekey') || ''; }
                    // Rendered by grecaptcha.render() rather than markup: the
                    // anchor iframe carries the key in its query string.
                    for (const frame of document.querySelectorAll('iframe')) {
                        const match = (frame.src || '').match(/[?&]k=([^&]+)/);
                        if (match) { return decodeURIComponent(match[1]); }
                    }
                    return '';
                }"""
            )
        except Exception:
            found = ""
        found = (found or "").strip()
        if found and found != FALLBACK_SITEKEY:
            self.log(f"[정보] 페이지에서 캡차 sitekey를 읽었습니다 · {found[:12]}…", "info")
        elif not found:
            self.log(
                "[경고] 페이지에서 캡차 sitekey를 찾지 못해 내장 값을 사용합니다.",
                "warning",
            )
        self._sitekey = found or FALLBACK_SITEKEY
        return self._sitekey

    async def _challenge_open(self, page):
        """True while the image challenge popup is on screen.

        Clicking the anchor again at that point dismisses the challenge, so this
        is the guard that stops the widget from being reset forever.
        """
        try:
            return bool(await page.evaluate(
                """() => {
                    for (const frame of document.querySelectorAll('iframe')) {
                        const src = frame.src || '';
                        if (!src.includes('recaptcha') || !src.includes('bframe')) {
                            continue;
                        }
                        // The popup wrapper is display:none until the challenge
                        // is actually shown.
                        let node = frame;
                        while (node && node !== document.body) {
                            const style = window.getComputedStyle(node);
                            if (style.display === 'none' || style.visibility === 'hidden') {
                                return false;
                            }
                            node = node.parentElement;
                        }
                        const box = frame.getBoundingClientRect();
                        return box.width > 0 && box.height > 0;
                    }
                    return false;
                }"""
            ))
        except Exception:
            return False

    async def _nudge_recaptcha_widget(self, page):
        """Click '로봇이 아닙니다' at most a couple of times, well spaced out.

        This used to be called on every pass of the watch loop -- up to twenty
        times a second near the open moment. Each click resets the widget and
        closes any challenge the user was working on, so the token could never
        appear and the run degenerated into clicking the checkbox forever. The
        click is now rate-limited, capped, skipped while a challenge is open,
        and skipped entirely when YesCaptcha is supplying the token, since an
        API token needs no widget interaction at all.
        """
        if self._anchor_clicks >= self.ANCHOR_CLICK_MAX:
            return False
        now = time.monotonic()
        if now - self._anchor_last_click < self.ANCHOR_CLICK_COOLDOWN:
            return False
        if await self._challenge_open(page):
            self._log_throttled(
                "challenge_open",
                "[정보] 이미지 퍼즐이 열려 있습니다. 브라우저에서 직접 풀어주세요.",
                "info",
                interval=20.0,
            )
            return False

        anchor = None
        for frame in page.frames:
            if "recaptcha/api2/anchor" in (frame.url or ""):
                anchor = frame
                break
        if anchor is None:
            self._log_throttled(
                "anchor_missing",
                "[경고] 캡차 위젯(anchor 프레임)을 찾지 못했습니다.",
                "warning",
                interval=30.0,
            )
            return False

        self._anchor_last_click = now
        self._anchor_clicks += 1
        try:
            await anchor.locator("#recaptcha-anchor").click(timeout=3000)
        except Exception as exc:
            self.log(f"[경고] '로봇이 아닙니다' 클릭 실패: {exc}", "warning")
            return False
        self.log(
            f"[캡차] '로봇이 아닙니다'를 클릭했습니다. "
            f"({self._anchor_clicks}/{self.ANCHOR_CLICK_MAX})",
            "info",
        )
        return True

    async def _captcha_token_value(self, page):
        """Return the token currently stored in the widget textarea.

        Reads the widget's own textarea only. grecaptcha.getResponse() is
        deliberately *not* consulted: the YesCaptcha path patches it, so asking
        it would report an injected token as if the widget had produced one and
        make expiry undetectable.
        """
        try:
            return str(await page.evaluate(
                """() => {
                    const areas = document.querySelectorAll(
                        'textarea[name="g-recaptcha-response"]'
                    );
                    for (const area of areas) {
                        if (area.value && area.value.length > 0) { return area.value; }
                    }
                    return '';
                }"""
            ) or "")
        except Exception:
            return ""

    async def _captcha_token_present(self, page):
        """Compatibility predicate used by tests and manual-captcha callers."""
        return bool(await self._captcha_token_value(page))

    def _manual_captcha_expired(self, captcha_since):
        return bool(
            captcha_since
            and time.monotonic() - captcha_since >= self.CAPTCHA_TTL_SECONDS
        )

    async def _reset_captcha_widget(self, page):
        try:
            await page.evaluate(
                """() => {
                    try {
                        if (window.grecaptcha) { window.grecaptcha.reset(); }
                    } catch (e) {}
                    document.querySelectorAll(
                        'textarea[name="g-recaptcha-response"]'
                    ).forEach((area) => { area.value = ''; });
                }"""
            )
        except Exception:
            pass

    @staticmethod
    def _is_manual_widget_token(widget_value, injected_token):
        """True only for a widget token that was not injected by this worker."""
        return bool(widget_value and widget_value != injected_token)

    # -- YesCaptcha token lifecycle -------------------------------------
    TOKEN_PATCH_SCRIPT = """(token) => {
        window.__pgCaptchaToken = token || '';
        const areas = document.querySelectorAll('textarea[name="g-recaptcha-response"]');
        areas.forEach((area) => { area.value = window.__pgCaptchaToken; });
        // The page validates with grecaptcha.getResponse() before FormData reads
        // the textarea. Patch only while an API token is active and restore the
        // original getter when that token is cleared. This prevents an OFF/manual
        // run from inheriting a synthetic getter.
        if (window.grecaptcha && window.__pgCaptchaToken) {
            if (!window.__pgCaptchaPatch) {
                window.__pgCaptchaPatch = {
                    owner: window.grecaptcha,
                    original: typeof window.grecaptcha.getResponse === 'function'
                        ? window.grecaptcha.getResponse
                        : null,
                };
            }
            window.grecaptcha.getResponse = function () {
                if (window.__pgCaptchaToken) { return window.__pgCaptchaToken; }
                const patch = window.__pgCaptchaPatch;
                try {
                    return patch && patch.original
                        ? patch.original.apply(patch.owner, arguments)
                        : '';
                }
                catch (e) { return ''; }
            };
        } else if (window.__pgCaptchaPatch) {
            const patch = window.__pgCaptchaPatch;
            if (patch.owner && patch.original) {
                patch.owner.getResponse = patch.original;
            }
            delete window.__pgCaptchaPatch;
        }
        return areas.length;
    }"""

    TOKEN_READY_SCRIPT = """(token) => {
        const form = document.querySelector('#form');
        if (!form || !token) {
            return { formFields: 0, formMatches: false, getterMatches: false };
        }
        const fields = form.querySelectorAll('textarea[name="g-recaptcha-response"]');
        let posted = '';
        try { posted = new FormData(form).get('g-recaptcha-response') || ''; }
        catch (e) { posted = ''; }
        let getter = '';
        try {
            getter = window.grecaptcha && typeof window.grecaptcha.getResponse === 'function'
                ? window.grecaptcha.getResponse()
                : '';
        } catch (e) { getter = ''; }
        return {
            formFields: fields.length,
            formMatches: fields.length > 0 && posted === token,
            getterMatches: getter === token,
        };
    }"""

    FINAL_CLICK_SCRIPT = """async (args) => {
        const slotId = String(args.slotId || '');
        const apiToken = String(args.apiToken || '');
        const apiTokenActive = Boolean(args.apiTokenActive);
        const fireAt = Number(args.fireAtClientEpochMs || 0);
        const armId = String(args.armId || '');
        if (armId) { window.__pgFinalArmId = armId; }
        const form = document.querySelector('#form');
        let written = 0;
        if (form) {
            for (const name of ['theme_time_num', 'themeTimeNum']) {
                for (const field of form.querySelectorAll(`[name="${name}"]`)) {
                    field.value = slotId;
                    written += 1;
                }
            }
        }

        let captchaReady = true;
        if (apiTokenActive) {
            window.__pgCaptchaToken = apiToken;
            const areas = document.querySelectorAll(
                'textarea[name="g-recaptcha-response"]'
            );
            areas.forEach((area) => { area.value = apiToken; });
            if (window.grecaptcha && apiToken) {
                if (!window.__pgCaptchaPatch) {
                    window.__pgCaptchaPatch = {
                        owner: window.grecaptcha,
                        original: typeof window.grecaptcha.getResponse === 'function'
                            ? window.grecaptcha.getResponse
                            : null,
                    };
                }
                window.grecaptcha.getResponse = function () {
                    if (window.__pgCaptchaToken) { return window.__pgCaptchaToken; }
                    const patch = window.__pgCaptchaPatch;
                    try {
                        return patch && patch.original
                            ? patch.original.apply(patch.owner, arguments)
                            : '';
                    } catch (e) { return ''; }
                };
            }
            let posted = '';
            let getter = '';
            try { posted = form ? new FormData(form).get('g-recaptcha-response') || '' : ''; }
            catch (e) { posted = ''; }
            try {
                getter = window.grecaptcha && typeof window.grecaptcha.getResponse === 'function'
                    ? window.grecaptcha.getResponse()
                    : '';
            } catch (e) { getter = ''; }
            captchaReady = Boolean(
                apiToken && form && areas.length > 0
                && posted === apiToken && getter === apiToken
            );
        }

        const button = document.querySelector('button.submit.pc')
            || document.querySelector('button.submit')
            || Array.from(document.querySelectorAll('button, a'))
                .find(el => (el.innerText || '').includes('예약하기'));
        const ready = Boolean(form && written > 0 && captchaReady && button);
        if (ready && fireAt > Date.now()) {
            // Yield for the long portion, then keep the final 25 ms inside the
            // browser process. Windows timer wake-ups can otherwise overshoot
            // the gate by roughly one scheduler quantum.
            const coarseDelay = Math.max(0, fireAt - Date.now() - 25);
            if (coarseDelay > 0) {
                await new Promise(resolve => setTimeout(resolve, coarseDelay));
            }
            while (Date.now() < fireAt) {}
        }
        const stillArmed = !armId || window.__pgFinalArmId === armId;
        if (ready && stillArmed) { button.click(); }
        return {
            written,
            captchaReady,
            buttonFound: Boolean(button),
            clicked: ready && stillArmed,
            armed: Boolean(fireAt),
            firedAtClientEpochMs: Date.now(),
        };
    }"""

    async def _write_token(self, page, token, *, quiet=False):
        """Put a token (or '' to drop one) into the form. Returns fields written."""
        try:
            # The token is passed as an argument, never interpolated into the
            # script source: a stray quote or backslash used to break the whole
            # evaluate() and the failure was swallowed silently.
            written = int(
                await page.evaluate(self.TOKEN_PATCH_SCRIPT, token or "") or 0
            )
        except Exception as exc:
            if self._is_browser_connection_error(exc):
                self._browser_connection_lost = True
                self._log_throttled(
                    "browser_connection_lost",
                    "[에러] Chrome 자동화 연결이 종료되었습니다. "
                    "이 예약 작업을 중단합니다. 다른 프로그램은 독립 Chrome 슬롯에서 계속 실행됩니다.",
                    "error",
                    interval=3600.0,
                )
            elif not quiet:
                self.log(f"[경고] 캡차 토큰 주입 실패: {exc}", "warning")
            return 0
        return written

    @staticmethod
    def _is_browser_connection_error(exc):
        message = str(exc or "").lower()
        return any(marker in message for marker in (
            "connection closed",
            "browser has been closed",
            "context or browser has been closed",
            "target page, context or browser has been closed",
            "while reading from the driver",
        ))

    async def _inject_yescaptcha_token(self, page, token):
        """Write and independently verify the exact value that the form will post."""
        if not (self._yescaptcha_active() and token):
            return False
        written = await self._write_token(page, token)
        if not written:
            return False
        try:
            state = await page.evaluate(self.TOKEN_READY_SCRIPT, token)
        except Exception as exc:
            self.log(f"[경고] 캡차 토큰 제출 상태 확인 실패: {exc}", "warning")
            return False
        state = state if isinstance(state, dict) else {}
        form_matches = bool(state.get("formMatches"))
        getter_matches = bool(state.get("getterMatches"))
        if not (form_matches and getter_matches):
            self.log(
                "[경고] YesCaptcha 토큰이 예약 폼과 캡차 검사 함수에 동일하게 반영되지 않았습니다. "
                f"(폼 필드 {int(state.get('formFields', 0) or 0)}개)",
                "warning",
            )
            return False
        return True

    def _yescaptcha_active(self):
        return bool(self._yc_enabled and self._yc_client_key)

    def _token_seconds_left(self):
        if not self._yc_token:
            return 0.0
        return self.CAPTCHA_TTL_SECONDS - (time.monotonic() - self._yc_token_at)

    def _drop_token(self):
        self._yc_token = ""
        self._yc_token_at = 0.0
        self._yc_token_test_only = False

    async def _retire_submitted_yescaptcha_token(self, page):
        """Drop a one-shot token after the booking button handed it to the site."""
        if not self._yc_token_submitted:
            return False
        self._drop_token()
        # This is best-effort cleanup after the one-shot response has already
        # been submitted. A page/driver disconnect here is not an injection
        # failure and must not turn a successful booking log into an error.
        await self._write_token(page, "", quiet=True)
        self._yc_token_submitted = False
        return True

    async def _cancel_yescaptcha_task(self):
        self._yc_cancel_event.set()
        task = self._yc_task
        self._yc_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _solve_with_yescaptcha(self, page, test_only=False):
        """One create + poll round trip. Stores the token on success."""
        try:
            await self._solve_with_yescaptcha_inner(page, test_only=test_only)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # This runs as a detached task: an escaping exception would only
            # surface as asyncio's "Task exception was never retrieved".
            self._yc_failures += 1
            self.log(f"[YesCaptcha] 해결 중 예외: {exc}", "warning")

    async def _solve_with_yescaptcha_inner(self, page, test_only=False):
        loop = asyncio.get_running_loop()
        client = YesCaptchaClient(self._yc_client_key, self._yc_soft_id)
        solve_started = time.monotonic()
        sitekey = await self._read_sitekey(page)
        if not sitekey:
            self._yc_failures += 1
            self.log("[YesCaptcha] 캡차 sitekey가 없어 API 요청을 중단했습니다.", "warning")
            return

        ok, task_id, err = await loop.run_in_executor(
            None, lambda: client.create_recaptcha_v2_task(self.reservation_url, sitekey)
        )
        if not ok:
            self._yc_failures += 1
            self.log(f"[YesCaptcha] 태스크 접수 실패: {err}", "warning")
            return
        self.log(
            f"[YesCaptcha] 태스크 생성 (ID {task_id}, 키 식별자 {client.key_fingerprint}) "
            "· 토큰 발급 대기 중…",
            "info",
        )

        poll_ok, token, poll_err = await loop.run_in_executor(
            None,
            lambda: client.poll_result(
                task_id,
                timeout_seconds=self.CAPTCHA_SOLVE_TIMEOUT,
                stop_event=self._yc_cancel_event,
            ),
        )
        if poll_ok and token and self._yescaptcha_active():
            self._yc_token = token
            self._yc_token_test_only = bool(test_only)
            # Timed from arrival, not from the request: the clock Google cares
            # about starts when the token is minted, and polling can take 30 s.
            self._yc_token_at = time.monotonic()
            self._yc_failures = 0
            solve_elapsed = time.monotonic() - solve_started
            self._remember_captcha_solve_time(solve_elapsed)
            self.log(
                f"[YesCaptcha] {'테스트용 ' if test_only else ''}API 토큰 발급 완료 · "
                f"발급 {solve_elapsed:.1f}초 · "
                f"약 {int(self._token_seconds_left())}초간 유효 "
                "(화면 체크 표시는 비어 있어도 정상이며, 폼 주입 상태를 별도로 검증합니다)",
                "info",
            )
        elif not self._yc_cancel_event.is_set():
            self._yc_failures += 1
            self.log(f"[YesCaptcha] 자동 해결 실패: {poll_err}", "warning")

    def _ensure_yescaptcha_token(self, page, remaining):
        """Start a solve round when one is due. Non-blocking.

        Requesting the token up front was the other half of the failure: a v2
        token dies in about two minutes, so a run started an hour before the
        window opened arrived at the submit with a corpse and never asked for
        another one.
        """
        if not self._yescaptcha_active():
            return
        if self._yc_failures >= self.YESCAPTCHA_MAX_FAILURES:
            self._log_throttled(
                "yc_giveup",
                f"[경고] YesCaptcha 자동 해결이 {self._yc_failures}회 실패했습니다. "
                "브라우저에서 직접 인증해주세요.",
                "warning",
                interval=30.0,
            )
            return
        if self._yc_task is not None and not self._yc_task.done():
            return
        self._yc_task = None
        if self._token_seconds_left() > self.CAPTCHA_REFRESH_MARGIN:
            return
        # Space out retries. Without this a network hiccup would consume the
        # whole failure budget in a few seconds.
        now = time.monotonic()
        if now - self._yc_last_attempt < self.YESCAPTCHA_RETRY_COOLDOWN:
            return
        solve_lead = self._captcha_lead_seconds()
        normal_due = remaining is None or remaining <= solve_lead
        test_due = (
            self._yc_test_mode
            and not self._yc_test_attempted
            and not normal_due
        )
        if not normal_due and not test_due:
            self._log_throttled(
                "yc_wait",
                f"[정보] 캡차 토큰은 오픈 {int(solve_lead)}초 전에 발급합니다. "
                "(토큰 수명이 약 2분이라 미리 받아두면 만료됩니다)",
                "info",
                interval=60.0,
            )
            return
        if test_due:
            self._yc_test_attempted = True
            self.log(
                "[YesCaptcha 테스트] 오픈 시각을 무시하고 즉시 1회 토큰 발급을 시작합니다. "
                "발급된 토큰은 실제 예약과 같은 경로로 주입·유지하며 반복 발급하지 않습니다.",
                "warning",
            )
        self._yc_last_attempt = time.monotonic()
        self._yc_cancel_event.clear()
        solve = (
            self._solve_with_yescaptcha(page, test_only=True)
            if test_due
            else self._solve_with_yescaptcha(page)
        )
        self._yc_task = asyncio.create_task(solve)

    # ------------------------------------------------------------------
    # Detection + submission
    # ------------------------------------------------------------------
    async def _watch_and_submit(
        self, page, dialog_state, reservation_data,
        target_date, target_time, zizum_num, theme_num, theme_name,
        slot_id,
    ):
        dev_mode = bool(reservation_data.get("devMode", False))
        submit_attempts = 0
        prompted = False
        captcha_ok = False
        captcha_since = 0.0
        # "manual" -> the widget produced it, "api" -> YesCaptcha did. Only the
        # api case has a known mint time, so only it can be expired on schedule.
        captcha_source = ""
        trusted_attempted = False
        last_resync = time.monotonic()
        restores = 0

        await self._cancel_yescaptcha_task()
        self._yc_cancel_event = threading.Event()
        self._drop_token()
        self._yc_failures = 0
        self._yc_last_attempt = 0.0
        self._yc_token_submitted = False
        self._yc_test_attempted = False
        (
            self._yc_enabled,
            self._yc_client_key,
            self._yc_soft_id,
        ) = self.read_yescaptcha_settings(reservation_data)
        self._yc_profile_key = self._captcha_profile_id(self._yc_client_key)
        self._captcha_solve_lead = self._load_captcha_solve_lead()
        solve_lead = self._captcha_lead_seconds()
        self._yc_test_mode = (
            self._yc_enabled
            and self.read_yescaptcha_test_mode(reservation_data)
        )

        if self._yc_enabled and not self._yc_client_key:
            self.log(
                "[경고] YesCaptcha가 켜져 있지만 API 키가 비어 있습니다. "
                "고급 설정에서 Client Key를 입력해주세요. 이번 실행은 수동 인증으로 진행합니다.",
                "warning",
            )
        if self._yc_enabled and self._yc_client_key:
            if self._yc_test_mode:
                self.log(
                    "[YesCaptcha 테스트 모드 ON] 시작 즉시 1회 발급·주입을 검증합니다. "
                    f"실제 예약용 토큰은 오픈 {int(solve_lead)}초 전에 "
                    f"별도로 발급합니다. (SoftID {self._yc_soft_id})",
                    "warning",
                )
            else:
                self.log(
                    f"[YesCaptcha] 자동 해결 대기 중 · 오픈 {int(solve_lead)}초 전에 "
                    f"토큰을 발급합니다. (SoftID {self._yc_soft_id})",
                    "info",
                )
            await self._read_sitekey(page)
        else:
            self.log(
                "[YesCaptcha OFF] API 호출, 토큰 주입, 캡차 체크박스 자동 클릭을 하지 않습니다. "
                "브라우저에서 직접 인증해주세요.",
                "warning",
            )

        if dev_mode:
            self.log(
                "[완료] [개발자 테스트] 화면 준비까지 완료했습니다. 제출은 하지 않습니다.",
                "success",
            )
            await self._dump_debug(page)
            while not self.stop_event.is_set():
                await asyncio.sleep(0.5)
            return

        while not self.stop_event.is_set():
            if self._browser_connection_lost:
                return
            # -- the site wiped the page? -------------------------------
            # Without this the loop would keep polling a document that has no
            # captcha and no form, reporting '캡차 미인증' forever.
            if await self._is_blocked(page):
                if restores >= self.MAX_PAGE_RESTORES:
                    self.log(
                        "[에러] 사이트가 개발자도구 차단 화면을 반복 표시합니다. "
                        "브라우저에서 직접 확인해주세요.",
                        "error",
                    )
                    return
                restores += 1
                self.log(
                    f"[경고] 사이트 개발자도구 차단 화면이 표시되어 예약 화면을 복구합니다. "
                    f"({restores}/{self.MAX_PAGE_RESTORES})",
                    "warning",
                )
                if not await self._restore_step_two(page, reservation_data):
                    return
                # A rebuilt document has a fresh widget and no patched getter,
                # so the injection has to happen again. The token itself is
                # still good, so it is kept -- only the page-side state resets.
                captcha_ok = False
                captcha_since = 0.0
                captcha_source = ""
                prompted = False
                self._anchor_clicks = 0
                self._anchor_last_click = 0.0
                await asyncio.sleep(0.2)
                continue

            # -- keep the server clock honest ----------------------------
            remaining = (
                self.clock.seconds_until(self.open_at)
                if self.open_at is not None else 0.0
            )
            if getattr(self, "_clock_sync_enabled", True) and (
                time.monotonic() - last_resync >= self.RESYNC_INTERVAL or (
                    0 < remaining <= self.FINAL_SYNC_LEAD and
                    time.monotonic() - last_resync >= 5.0
                )
            ):
                last_resync = time.monotonic()
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._sync_server_clock(announce=False)
                )
                remaining = (
                    self.clock.seconds_until(self.open_at)
                    if self.open_at is not None else 0.0
                )

            # -- captcha ------------------------------------------------
            # `remaining` is None when the open moment is unknown, which the
            # token scheduler reads as "submit as soon as possible".
            lead = None if self.open_at is None else remaining
            self._ensure_yescaptcha_token(page, lead)

            widget_token_value = await self._captcha_token_value(page)
            # Our own injected token also lives in this textarea. Comparing only
            # its presence made the next loop call it a "manual solve", discard
            # the API token, then announce 인증/해제 back-to-back. A manual token
            # is one whose exact value differs from the token we injected.
            manual_widget_token = self._is_manual_widget_token(
                widget_token_value, self._yc_token
            )
            if (
                captcha_source != "api"
                and self._yc_token_test_only
                and self._token_seconds_left() <= 0
            ):
                self.log(
                    "[YesCaptcha 테스트 실패] 테스트 토큰이 폼 검증 전에 만료되어 폐기합니다.",
                    "warning",
                )
                self._drop_token()
            elif manual_widget_token and self._yc_token_test_only:
                self.log(
                    "[YesCaptcha 테스트 건너뜀] 브라우저에 수동 캡차 토큰이 이미 있어 "
                    "덮어쓰지 않고 테스트 토큰을 폐기합니다.",
                    "warning",
                )
                self._drop_token()

            if captcha_source == "api":
                # An injected token has a known mint time, so it can be retired
                # on schedule instead of being re-submitted until the site
                # rejects it. Dropping it also clears the patched getter, so a
                # manual solve takes over cleanly.
                if self._token_seconds_left() <= 0:
                    test_token = self._yc_token_test_only
                    self.log(
                        (
                            "[YesCaptcha 테스트 종료] 실제 예약과 동일한 활성 상태 확인을 "
                            "마쳤으며 테스트 토큰이 만료되어 예약 폼에서 제거합니다."
                            if test_token else
                            "[경고] YesCaptcha 토큰이 만료됐습니다. 새 토큰을 발급합니다."
                        ),
                        "info" if test_token else "warning",
                    )
                    self._drop_token()
                    await self._write_token(page, "")
                    captcha_ok = False
                    captcha_since = 0.0
                    captcha_source = ""
                elif self._yc_token and self._token_seconds_left() > 0:
                    left = self._token_seconds_left()
                    if left <= self.CAPTCHA_WARN_SECONDS:
                        self._log_throttled(
                            "captcha_expiry",
                            f"[경고] YesCaptcha 토큰 유효시간 약 {max(0, int(left))}초 남음",
                            "warning",
                            interval=10.0,
                        )

            if not captcha_ok:
                if manual_widget_token:
                    # The person (or a plain checkbox pass) solved it.
                    captcha_ok = True
                    captcha_source = "manual"
                    captcha_since = time.monotonic()
                    self.log(
                        "[수동 캡차] 인증 완료 · 예약 제출 준비됨",
                        "success",
                    )
                elif (
                    self._yescaptcha_active()
                    and self._yc_token
                    and self._token_seconds_left() > 0
                ):
                    test_token = self._yc_token_test_only
                    if await self._inject_yescaptcha_token(page, self._yc_token):
                        captcha_ok = True
                        captcha_source = "api"
                        captcha_since = time.monotonic()
                        if test_token:
                            self.log(
                                "[YesCaptcha 테스트 확인] 실제 예약과 동일한 토큰 주입·폼 검증 "
                                f"경로를 통과했습니다 · 약 {int(self._token_seconds_left())}초간 "
                                "활성 상태를 유지합니다. 사이트 화면의 체크 표시가 비어 있어도 "
                                "해결된 상태이며, 최종 승인은 실제 제출 응답에서 확정됩니다.",
                                "success",
                            )
                        else:
                            self.log(
                                f"[YesCaptcha] 예약 폼 토큰 준비 확인 · 약 "
                                f"{int(self._token_seconds_left())}초간 유효 "
                                "· 화면 체크 표시는 비어 있어도 해결된 상태입니다 "
                                "(최종 승인은 사이트 제출 응답으로 확인)",
                                "info",
                            )
                    else:
                        if test_token:
                            self.log(
                                "[YesCaptcha 테스트 실패] 발급된 토큰이 예약 폼에 정상 반영되지 않았습니다. "
                                "테스트 토큰을 폐기합니다.",
                                "warning",
                            )
                            self._drop_token()
                            await self._write_token(page, "")
                        self._log_throttled(
                            "inject_fail",
                            "[경고] 캡차 응답 필드를 찾지 못해 토큰을 주입할 수 없습니다.",
                            "warning",
                            interval=15.0,
                        )
            elif captcha_source == "manual" and not manual_widget_token:
                captcha_ok = False
                captcha_since = 0.0
                captcha_source = ""
                self.log("[경고] 캡차 인증이 해제되었습니다. 다시 인증해주세요.", "warning")
            elif captcha_source == "manual":
                left = self.CAPTCHA_TTL_SECONDS - (time.monotonic() - captcha_since)
                if self._manual_captcha_expired(captcha_since):
                    await self._reset_captcha_widget(page)
                    captcha_ok = False
                    captcha_since = 0.0
                    captcha_source = ""
                    prompted = False
                    self.log(
                        "[경고] 수동 캡차의 안전 유효시간이 지나 자동 초기화했습니다. "
                        "새로 인증해주세요.",
                        "warning",
                    )
                elif left <= self.CAPTCHA_WARN_SECONDS:
                    self._log_throttled(
                        "captcha_expiry",
                        f"[경고] 캡차 유효시간 약 {max(0, int(left))}초 남음",
                        "warning",
                        interval=10.0,
                    )

            # -- ask for the captcha at the right moment -----------------
            if not prompted and (
                self.open_at is None or remaining <= self.CAPTCHA_PROMPT_LEAD
            ):
                prompted = True
                if self._yescaptcha_active():
                    self.log(
                        "[정보] 오픈이 가까워졌습니다. 캡차는 YesCaptcha가 자동으로 처리합니다.",
                        "info",
                    )
                else:
                    self.log(
                        "[경고] 지금 브라우저에서 '로봇이 아닙니다'를 인증해주세요. "
                        "인증은 약 2분간만 유효합니다.",
                        "warning",
                    )
                try:
                    await page.bring_to_front()
                except Exception:
                    pass

            # -- still waiting -------------------------------------------
            if self.open_at is not None and remaining > self.FIRE_LEAD:
                if captcha_ok and remaining <= self.FINAL_QUIET_LEAD_SECONDS:
                    self._log_throttled(
                        "final_quiet",
                        "[정보] 캡차 준비 완료 · 오픈 직전 경량 대기로 전환합니다.",
                        "info", interval=60.0,
                    )
                    await self._wait_for_open_quiet()
                    if self.stop_event.is_set():
                        return
                    # Fall through in this same loop turn. This deliberately
                    # avoids another blocked-page/captcha/DOM check after T0.
                    remaining = self.clock.seconds_until(self.open_at)
                else:
                    if remaining > self.FINAL_QUIET_LEAD_SECONDS:
                        self.silent_tick(f"{target_time} 오픈 대기")
                    self._log_throttled(
                        "waiting",
                        f"[정보] 서버 시간 기준 오픈까지 "
                        f"{self._format_remaining(remaining)} 남음"
                        + (
                            ""
                            if captcha_ok
                            else (
                                " · YesCaptcha 토큰 발급 대기"
                                if self._yescaptcha_active()
                                else " · 수동 캡차 미인증"
                            )
                        ),
                        "info",
                        interval=30.0 if remaining > 180 else 10.0,
                    )
                    await asyncio.sleep(
                        self.POLL_NEAR_SECONDS if remaining <= 3.0
                        else min(self.POLL_IDLE_SECONDS, max(0.05, remaining - 3.0))
                    )
                    continue

            # -- the moment has arrived ----------------------------------
            use_trusted_slot = bool(
                self._trusted_slot_id and not trusted_attempted
            )
            if use_trusted_slot:
                live_slot_id = self._trusted_slot_id
                live_slot_status = "trusted"
                self._trace_timing(
                    f"검증 시간표 선발사 준비 · ID {live_slot_id} · "
                    f"기준 {', '.join(self._trusted_slot_sources)}"
                )
            else:
                live_slot_id, live_slot_status = await self._resolve_live_slot(
                    target_date, target_time, zizum_num, theme_num, slot_id
                )
                if self._page_success_event.is_set():
                    return
            if live_slot_status == "capacity":
                await self._report_capacity_result()
                return
            if not live_slot_id:
                self._log_throttled(
                    "await_live_slot",
                    "[정보] 오픈 시각 도달 · 대상 날짜의 실제 슬롯 ID 공개를 기다립니다.",
                    "info", interval=1.0,
                )
                await asyncio.sleep(self._live_slot_retry_delay())
                continue
            slot_id = live_slot_id

            if not captcha_ok:
                automatic = self._yescaptcha_active()
                self._log_throttled(
                    "await_captcha",
                    (
                        "[YesCaptcha] 오픈 시각 도달 · 이 페이지는 토큰 발급 대기 중이며 "
                        "준비되는 즉시 제출합니다."
                        if automatic else
                        "[경고] 오픈 시각이 되었지만 수동 캡차 인증이 없습니다. "
                        "인증하시면 즉시 제출합니다."
                    ),
                    "info" if automatic else "warning",
                    interval=5.0,
                )
                await asyncio.sleep(0.1)
                continue

            if submit_attempts >= self.SUBMIT_MAX_ATTEMPTS:
                self.log(
                    f"[에러] 제출을 {submit_attempts}회 시도했으나 완료되지 못했습니다. "
                    "브라우저에서 직접 확인해주세요.",
                    "error",
                )
                return

            # Every standby page owns an independent form and captcha token.
            # Do not serialize them through BaseEngine.submission_lock: firing
            # all ready pages in the same event-loop turn is the purpose of this
            # mode. A first success stops pages that have not submitted yet,
            # while requests already handed to the site finish collecting their
            # own result below.
            submit_attempts += 1
            if use_trusted_slot:
                trusted_attempted = True
            dialog_state["message"] = ""
            result = await self._submit(
                page, dialog_state, slot_id, theme_name,
                target_date, target_time, zizum_num, dev_mode,
                api_token_active=(captcha_source == "api"),
                captcha_age=(
                    time.monotonic() - captcha_since if captcha_since else None
                ),
                fire_at_client_epoch_ms=(
                    self._trusted_fire_client_epoch_ms() if use_trusted_slot else 0.0
                ),
            )

            submitted_api_token = (
                captcha_source == "api" and self._yc_token_submitted
            )
            if result == "success":
                return
            if submitted_api_token:
                # A reCAPTCHA response is one-shot. Once the click handed it to
                # the site it must never be re-used, even when the response was
                # "not open", an unknown error, or a completion timeout.
                captcha_ok = False
                captcha_since = 0.0
                captcha_source = ""
                await self._retire_submitted_yescaptcha_token(page)
                self.log(
                    "[YesCaptcha] 토큰을 사이트에 제출해 일회용으로 소모 처리했습니다.",
                    "info",
                )
            if result == "capacity":
                await self._report_capacity_result()
                return
            if result == "submission_uncertain":
                self.log(
                    "[중복 방지 정지] 예약 POST가 서버로 전달된 뒤 결과를 확정하지 "
                    "못했습니다. 같은 페이지에서 다시 제출하지 않고 브라우저의 예약 "
                    "결과를 확인해주세요.",
                    "error",
                )
                return

            message = dialog_state.get("message", "")
            if result == "not_open":
                # The rejection carries the server's own open time; trust it over
                # anything parsed from the notice or the bundled table.
                corrected = self._open_time_from_message(message, target_date)
                if corrected is not None and corrected != self.open_at:
                    update_open_at = getattr(
                        self, "_open_at_update_callback", None
                    )
                    if update_open_at is not None:
                        update_open_at(corrected)
                    else:
                        self.open_at = corrected
                    self.log(
                        "[정보] 서버가 알려준 오픈 시각으로 일정을 재조정했습니다 · "
                        f"{self._format_remaining(self.clock.seconds_until(corrected))} 남음",
                        "info",
                    )
                    prompted = False
                else:
                    self.log("[정보] 아직 오픈 전입니다. 대기를 계속합니다.", "info")
                    await asyncio.sleep(0.5)
            elif result in ("captcha_consumed", "invalid_request"):
                captcha_ok = False
                captcha_since = 0.0
                was_api = submitted_api_token or captcha_source == "api"
                captcha_source = ""
                if was_api and not submitted_api_token:
                    self._drop_token()
                    await self._write_token(page, "")
                if was_api:
                    self.log(
                        "[경고] 사이트가 요청을 승인하지 않았습니다. 사용한 YesCaptcha "
                        "토큰은 폐기하고 실제 슬롯 ID를 유지한 채 새 토큰을 요청합니다.",
                        "warning",
                    )
                else:
                    await self._reset_captcha_widget(page)
                    self.log(
                        "[경고] 제출 요청이 거절되어 수동 캡차를 초기화했습니다. "
                        "실제 슬롯 ID는 확인됐으므로 캡차만 다시 인증해주세요.",
                        "warning",
                    )
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
            elif result == "captcha_not_ready":
                captcha_ok = False
                captcha_since = 0.0
                captcha_source = ""
            await asyncio.sleep(0.2)

    async def _report_capacity_result(self):
        """Distinguish a sibling page losing to our winner from a true loss."""
        if self._page_count > 1 and not self._page_success_event.is_set():
            await asyncio.sleep(self.SIBLING_SUCCESS_GRACE_SECONDS)
        if self._page_success_event.is_set():
            self.log(
                "[정보] 같은 실행의 다른 핫 스탠바이 페이지가 먼저 예약에 성공했습니다. "
                "이 페이지의 '이미 예약 완료' 응답은 정상이며 추가 제출 없이 종료합니다.",
                "info",
            )
            return "sibling"
        self.log(
            "[에러] 다른 실행 또는 다른 사용자가 먼저 예약을 완료한 시간대입니다. "
            "이번 회차는 마감되었습니다.",
            "error",
        )
        return "external"

    def _open_time_from_message(self, message, target_date):
        """Recompute the open moment from a '예약오픈시간 : HH:MM' rejection."""
        match = SERVER_OPEN_TIME_PATTERN.search(message or "")
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        # The rejection is produced on the day the window would open, so the
        # server's current date is the right one to attach the time to.
        now = datetime.fromtimestamp(self.clock.now(), KST)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate < now - timedelta(minutes=5):
            candidate += timedelta(days=1)
        return candidate.timestamp()

    async def _submit(
        self, page, dialog_state, slot_id, theme_name,
        target_date, target_time, zizum_num, dev_mode,
        api_token_active=False, captcha_age=None,
        fire_at_client_epoch_ms=0.0,
    ):
        slot_id = str(slot_id or "").strip()
        if not slot_id or slot_id == self.PLACEHOLDER_SLOT_ID:
            self.log(
                "[경고] 대상 날짜의 실제 슬롯 ID가 없어 제출을 중단했습니다. "
                f"(날짜 {target_date}, 시간 {target_time}, 슬롯 {slot_id or '빈 값'})",
                "warning",
            )
            return "slot_not_ready"
        self._yc_token_submitted = False
        dialog_state.update(self._new_submission_state())
        # One CDP round trip owns the complete final browser action: update both
        # possible slot-id spellings, re-stamp/verify an API captcha token when
        # one is active, and click the site's existing AJAX submit handler.
        try:
            arm_id = (
                f"{id(page)}-{time.time_ns()}" if fire_at_client_epoch_ms else ""
            )
            self._trace_timing(
                (
                    f"브라우저 예약 타이머 설치 · 슬롯 ID {slot_id}"
                    if fire_at_client_epoch_ms else
                    f"최종 브라우저 동작 전달 · 슬롯 ID {slot_id}"
                )
            )
            action = await self._run_final_click_action(
                page,
                {
                    "slotId": slot_id,
                    "apiToken": (
                        self._yc_token
                        if api_token_active and self._token_seconds_left() > 0
                        else ""
                    ),
                    "apiTokenActive": bool(api_token_active),
                    "fireAtClientEpochMs": float(fire_at_client_epoch_ms or 0.0),
                    "armId": arm_id,
                },
            )
        except Exception as exc:
            if self._is_browser_connection_error(exc):
                self._browser_connection_lost = True
            self.log(f"[경고] 최종 예약 동작 실행 실패: {exc}", "warning")
            return "retry"

        action = action if isinstance(action, dict) else {}
        written = int(action.get("written", 0) or 0)
        if not written:
            self.log(
                "[에러] 예약 폼에서 슬롯 ID 필드를 찾지 못했습니다. 페이지 구조가 바뀐 것 같습니다.",
                "error",
            )
            await self._dump_debug(page)
            return "retry"

        if api_token_active and not action.get("captchaReady"):
            self.log(
                "[경고] YesCaptcha 토큰이 실제 제출 폼에 준비되지 않아 클릭을 중단했습니다.",
                "warning",
            )
            return "captcha_not_ready"

        if not action.get("buttonFound"):
            self.log("[경고] 예약하기 버튼을 찾지 못했습니다.", "warning")
            await self._dump_debug(page)
            return "retry"

        if not action.get("clicked"):
            self.log("[경고] 최종 예약 조건이 준비되지 않아 클릭을 중단했습니다.", "warning")
            return "retry"

        age_text = (
            f", 캡차 경과 {captcha_age:.1f}초"
            if captcha_age is not None else ""
        )
        self.log(
            f"예약하기를 클릭합니다. (실제 슬롯 ID {slot_id}, "
            f"필드 {written}개 갱신{age_text})",
            "info",
        )
        self._trace_timing("예약 버튼 클릭 완료")
        self._yc_token_submitted = bool(api_token_active)

        completion = await self._await_completion(
            page,
            include_context_pages=(self._page_count <= 1),
            finish_inflight=True,
            submission_state=dialog_state,
        )
        if completion is None:
            if (
                dialog_state.get("request_started")
                and not dialog_state.get("submission_status")
                and not dialog_state.get("message")
            ):
                self.log(
                    "[정보] 예약 POST 전송을 확인했지만 응답이 늦어 추가 완료 확인을 "
                    f"{self.SUBMISSION_RECONCILE_SECONDS:.0f}초 진행합니다.",
                    "warning",
                )
                completion = await self._await_completion(
                    page,
                    timeout=self.SUBMISSION_RECONCILE_SECONDS,
                    include_context_pages=(self._page_count <= 1),
                    finish_inflight=True,
                    submission_state=dialog_state,
                )
                if completion is None:
                    return "submission_uncertain"
            else:
                return self._classify_failure(dialog_state.get("message", ""))

        self._page_success_event.set()
        booking_number = await self._resolve_booking_number(
            completion, dialog_state
        )
        if dialog_state.get("submission_status") == "success":
            self.log("[완료] 서버에서 예약 성공 응답을 확인했습니다.", "success")
        else:
            self.log("[완료] 예약 완료 화면을 확인했습니다.", "success")
        if booking_number:
            self.log(f"★ 예약번호 {booking_number} ★", "success")
        try:
            append_history({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "site": "키이스케이프",
                "branch": zizum_num,
                "date": target_date,
                "time": target_time,
                "theme": theme_name,
                "booking_number": booking_number,
            })
        except Exception as exc:
            self.log(f"[경고] 예약 내역 저장 실패: {exc}", "warning")
        self.notify_success(
            BookingResult(
                True,
                f"키이스케이프 예약 완료 · 예약번호 {booking_number or '확인 필요'}",
                booking_number=booking_number,
            )
        )
        return "success"

    async def _run_final_click_action(self, page, args):
        """Run a pre-armed browser click while keeping stop semantics authoritative."""
        fire_at = float(args.get("fireAtClientEpochMs") or 0.0)
        arm_id = str(args.get("armId") or "")
        if not fire_at:
            return await page.evaluate(self.FINAL_CLICK_SCRIPT, args)
        if self.stop_event.is_set() or self._page_success_event.is_set():
            return {
                "written": 1,
                "captchaReady": True,
                "buttonFound": True,
                "clicked": False,
                "cancelled": True,
            }

        action_task = asyncio.create_task(
            page.evaluate(self.FINAL_CLICK_SCRIPT, args),
            name=f"keyescape-final-arm-{getattr(self, '_page_index', 1)}",
        )
        while not action_task.done():
            if self.stop_event.is_set() or self._page_success_event.is_set():
                try:
                    await page.evaluate(
                        """(armId) => {
                            if (window.__pgFinalArmId === armId) {
                                window.__pgFinalArmId = '';
                            }
                        }""",
                        arm_id,
                    )
                except Exception:
                    pass
                return await action_task
            await asyncio.wait({action_task}, timeout=0.02)
        return await action_task

    @staticmethod
    def _classify_failure(message):
        """Map a site message onto a recovery action.

        Ordered most specific first. '이미 완료되었습니다' has to be tested before
        the window patterns, because the generic '아닙니다' / '예약 가능' tokens
        would otherwise swallow messages that mean the slot is simply gone -- and
        retrying a taken slot is pointless.

        Wordings observed in practice:
          [에러] 예약 가능 한 날짜가 아닙니다.            -> window not open
          예약가능시간이 아닙니다. 예약오픈시간 : 11:00     -> window not open
          [에러] 잘못된 접근입니다.                      -> no/expired captcha
          [예약불가] 예약이 이미 완료되었습니다.           -> slot taken
        """
        text = message or ""
        if not text:
            return "retry"
        if any(token in text for token in ("이미 완료", "이미 예약", "정원", "매진", "마감")):
            return "capacity"
        if "접근" in text:
            return "invalid_request"
        if any(token in text for token in ("자동등록방지", "자동입력", "로봇", "캡차", "보안문자")):
            return "captcha_consumed"
        if any(token in text for token in
               ("날짜가 아닙니다", "예약가능시간", "예약오픈시간", "예약 가능", "아닙니다")):
            return "not_open"
        return "retry"

    COMPLETION_URL_TOKEN = "reservation3.php"
    # Step 2 already contains the words '예약 완료' (the STEP 3 label and the
    # "예약완료시 문자가 발송됩니다" notice), so text matching would report success
    # the instant it was called. Only markup unique to reservation3.php counts:
    # the result block, the step-3 state and the 예약번호 row.
    COMPLETION_DOM_PROBE = """() => {
        if (!document.body) { return false; }
        if (document.querySelector('.resrv_result2')) { return true; }
        if (document.querySelector('.resrvStep.step3')) { return true; }
        const rows = document.querySelectorAll('.resrv_info_list li span.subject');
        for (const cell of rows) {
            if ((cell.innerText || '').replace(/\\s/g, '').includes('예약번호')) {
                return true;
            }
        }
        return false;
    }"""

    async def _await_completion(
        self,
        page,
        timeout=8.0,
        *,
        include_context_pages=True,
        finish_inflight=False,
        submission_state=None,
    ):
        """Return the page showing the completion screen, or None.

        Waiting on the original page's URL alone was too narrow: if the site
        answers in a new tab, a single-page run could treat a booking that
        actually went through as a failure and keep resubmitting. In hot-standby
        mode, however, scanning every sibling page makes concurrent requests
        steal each other's success screen. Each worker therefore watches only
        its own page (and a popup whose opener is that page). Requests already
        sent may also finish this check after another page records first success.
        """
        deadline = time.monotonic() + timeout
        while True:
            if submission_state:
                status = submission_state.get("submission_status", "")
                if status == "success":
                    return page
                if status == "failure":
                    return None

            candidates = [page]
            try:
                context_pages = list(page.context.pages)
            except Exception:
                context_pages = [page]
            if include_context_pages:
                candidates = context_pages
                if page not in candidates:
                    candidates.append(page)
            else:
                # Keep support for a site-owned completion popup without letting
                # one standby page claim a sibling standby page's reservation.
                for candidate in context_pages:
                    if candidate is page:
                        continue
                    try:
                        opener = await candidate.opener()
                    except Exception:
                        opener = None
                    if opener is page:
                        candidates.append(candidate)

            for candidate in candidates:
                try:
                    url = candidate.url or ""
                except Exception:
                    continue
                if self.COMPLETION_URL_TOKEN in url:
                    return candidate
                try:
                    if await candidate.evaluate(self.COMPLETION_DOM_PROBE):
                        return candidate
                except Exception:
                    continue

            if (
                time.monotonic() >= deadline
                or (self.stop_event.is_set() and not finish_inflight)
            ):
                return None
            await asyncio.sleep(0.2)

    async def _resolve_booking_number(self, page, dialog_state, timeout=3.0):
        """Let the AJAX response/redirect finish before declaring the number absent.

        Keyescape shows its success dialog before the response listener and the
        reservation3 DOM are guaranteed to have settled. This short post-success
        wait does not affect booking speed; the server has already accepted it.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            number = str(dialog_state.get("booking_number", "") or "").strip()
            if number:
                return number
            try:
                number = str(await self._extract_booking_number(page) or "").strip()
            except Exception:
                number = ""
            if number:
                dialog_state["booking_number"] = number
                return number
            if time.monotonic() >= deadline:
                return ""
            await asyncio.sleep(0.1)

    async def _extract_booking_number(self, page):
        # The completion page renders it as a pair of spans:
        #   <span class="subject">예약번호</span><span class="width_80">15490</span>
        # Reading the DOM directly survives whitespace and layout changes that
        # a text regex would trip over.
        try:
            value = await page.evaluate(
                """() => {
                    const rows = document.querySelectorAll('.resrv_info_list li');
                    for (const row of rows) {
                        const cells = row.querySelectorAll('span');
                        if (cells.length >= 2 &&
                            (cells[0].innerText || '').replace(/\\s/g, '').includes('예약번호')) {
                            return (cells[1].innerText || '').trim();
                        }
                    }
                    return '';
                }"""
            )
            if value:
                return value
        except Exception:
            pass

        try:
            body = await page.inner_text("body", timeout=5000)
        except Exception:
            return ""
        match = re.search(r"(?:예약\s*번호|예약번호)\s*[:：]?\s*([A-Za-z0-9-]+)", body)
        if not match:
            match = re.search(r"\bK\d{8}-\d+\b", body)
        if not match:
            return ""
        return match.group(1) if match.groups() else match.group(0)

    async def _dump_debug(self, page):
        try:
            html = await page.content()
            path = data_path("keyescape_step2_debug.html")
            write_redacted_debug_text(path, html)
            self.log(
                f"[정보] 민감정보를 제거한 현재 화면 HTML을 저장했습니다: {path}",
                "info",
            )
        except Exception as exc:
            # Debug capture is best-effort, but its own failure must still be
            # diagnosable now that these logs persist across application runs.
            self.log(
                f"[경고] 키이스케이프 디버그 HTML 저장 실패: "
                f"{type(exc).__name__}: {str(exc).strip() or '상세 메시지 없음'}",
                "warning",
            )
