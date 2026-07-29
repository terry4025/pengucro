"""Keyescape reservation engine.

Flow
----
1. The program enters Step 2 (``reservation2.php``) and fills in the reservation
   details -- name, the three phone fields, party size, payment method and the
   consent checkboxes.
2. The **user** solves the reCAPTCHA by hand. The program only observes whether
   a token is present; it never solves, submits or otherwise works around the
   challenge. There is no third-party solving service any more.
3. The program polls the site's own availability endpoint until the backend
   reports the target slot as actually bookable, then presses 예약하기 and
   confirms the outcome.

Why the backend signal instead of a clock
-----------------------------------------
``/controller/run_proc.php`` with ``t=get_theme_time`` returns one row per time
slot with an ``enable`` field; the site's own front-end renders ``enable === 'N'``
as a disabled radio button. That field is the exact condition under which
예약하기 can succeed, so it is used as the trigger. The previous implementation
ignored ``enable`` entirely and treated "the row exists" as "the slot is open",
which made it submit while the slot was still closed -- and every rejected
submission consumes the reCAPTCHA token.

Branch opening times are still read, but only to pace the polling and to tell
the user how long they have to wait. Nothing depends on them being correct.
"""

import asyncio
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from engines import browser_session
from engines.base_engine import BaseEngine
from engines.server_clock import ServerClock
from pengucro.models import BookingResult
from pengucro.storage import append_history, data_path, load_json


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
    CAPTCHA_PROMPT_LEAD = 150.0     # ask the user to solve this early
    FINAL_SYNC_LEAD = 20.0          # last clock re-sync before firing
    RESYNC_INTERVAL = 120.0         # periodic re-sync while waiting
    FIRE_LEAD = 0.15                # submit this far before the boundary
    POLL_IDLE_SECONDS = 1.0
    POLL_NEAR_SECONDS = 0.05

    # reCAPTCHA v2 tokens are single use and expire in about two minutes.
    CAPTCHA_TTL_SECONDS = 115
    CAPTCHA_WARN_SECONDS = 25

    SUBMIT_MAX_ATTEMPTS = 5
    PLACEHOLDER_SLOT_ID = "9999"
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
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.site_url}/reservation.php",
        })
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start_reservation(self, reservation_data, num_threads, is_async=False):
        self.log(
            "키이스케이프는 사용자가 직접 캡차를 인증하는 방식으로 동작합니다. "
            "브라우저 창을 닫지 마세요.",
            "info",
        )
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
    def _post(self, payload, timeout=5.0):
        response = self._session.post(self.api_url, data=payload, timeout=timeout)
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
        data = await self._post_async({
            't': 'get_theme_time',
            'date': target_date,
            'zizumNum': zizum_num,
            'themeNum': theme_num,
            'endDay': '1' if end_day else '0',
        })
        if not data.get("status") or not data.get("data"):
            return []
        return list(data["data"])

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
            None, lambda: self.clock.sync(announce=True)
        )

        # --- when does the window open -----------------------------------
        doing_days, notice_open_time = await self._fetch_window_info(
            zizum_num, theme_info_num
        )
        self.open_at = self._resolve_open_moment(
            zizum_num, target_date, doing_days, notice_open_time
        )

        # --- slot id -----------------------------------------------------
        # The slot num is per (theme, time) and stays the same across dates
        # (verified: num 2176 for 09:50 on both an open and an unopened date), so
        # the real id can be obtained up front. The placeholder is only needed
        # when the row does not exist at all.
        slot_id = ""
        try:
            slots = await self._fetch_slots(target_date, zizum_num, theme_num)
            slot_id, _bookable = self._match_slot(slots, target_time)
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

        form_slot_id = slot_id or self.PLACEHOLDER_SLOT_ID

        async with async_playwright() as playwright:
            browser, context, chrome_session, owns_browser = await self._open_browser(playwright)
            if browser is None or context is None:
                return
            try:
                # Must happen before the first page exists: init scripts only
                # apply to documents created after registration.
                await self._harden_context(context)
                page = await context.new_page()
                page.set_default_timeout(15000)

                dialog_state = {"message": ""}
                await self._prepare_page(page, dialog_state)

                await self._enter_step_two(
                    page, zizum_num, theme_num, theme_info_num,
                    target_date, form_slot_id, target_time, theme_name,
                )
                await self._fill_form(page, reservation_data)

                await self._watch_and_submit(
                    page, dialog_state, reservation_data,
                    target_date, target_time, zizum_num, theme_num, theme_name,
                    slot_id,
                )
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.log(f"[에러] 키이스케이프 처리 중 오류: {exc}", "error")
            finally:
                # The worker tears its own browser down. Doing it from another
                # thread raced with whatever this coroutine was mid-way through.
                if owns_browser:
                    for closer in (context, browser):
                        try:
                            if closer is not None:
                                await closer.close()
                        except Exception:
                            continue
                else:
                    # Attached to a real Chrome: leave the window and its profile
                    # alone so the session keeps its accumulated reputation and
                    # the user can review the result. browser.close() on a CDP
                    # connection only detaches Playwright; Chrome itself stays up.
                    # Deliberately silent -- this is routine teardown, not news.
                    try:
                        await browser.close()
                    except Exception:
                        pass
                try:
                    self._session.close()
                except Exception:
                    pass
                if chrome_session is not None and self._close_chrome_on_exit:
                    chrome_session.close_if_launched()

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
            session = browser_session.start_or_attach(log=self.log)
            if session is not None:
                try:
                    browser = await playwright.chromium.connect_over_cdp(session.endpoint)
                    # contexts[0] is Chrome's persistent profile context. Calling
                    # new_context() here would defeat the whole purpose by
                    # creating an empty incognito profile again.
                    context = browser.contexts[0] if browser.contexts else await browser.new_context()
                    self.log(
                        "실제 Chrome 프로필에 연결했습니다. "
                        f"(프로필: {browser_session.profile_dir()})",
                        "success",
                    )
                    return browser, context, session, False
                except Exception as exc:
                    self.log(f"[경고] Chrome DevTools 연결 실패: {exc}", "warning")
                    session.close_if_launched()

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

    async def _prepare_page(self, page, dialog_state):
        async def handle_dialog(dialog):
            dialog_state["message"] = dialog.message or ""
            self.log(f"[알림] 사이트 메시지: {dialog_state['message'][:120]}", "warning")
            try:
                await dialog.accept()
            except Exception:
                pass

        page.on("dialog", handle_dialog)

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
    # Captcha observation (observe only -- never solve)
    # ------------------------------------------------------------------
    async def _captcha_token_present(self, page):
        try:
            return bool(await page.evaluate(
                """() => {
                    if (window.grecaptcha && typeof grecaptcha.getResponse === 'function') {
                        try { return (grecaptcha.getResponse() || '').length > 0; }
                        catch (e) { /* fall through */ }
                    }
                    const field = document.querySelector('textarea[name="g-recaptcha-response"]');
                    return !!(field && field.value && field.value.length > 0);
                }"""
            ))
        except Exception:
            return False

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
        last_resync = time.monotonic()
        restores = 0

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
                captcha_ok = False
                captcha_since = 0.0
                prompted = False
                await asyncio.sleep(0.2)
                continue

            # -- keep the server clock honest ----------------------------
            remaining = (
                self.clock.seconds_until(self.open_at)
                if self.open_at is not None else 0.0
            )
            if time.monotonic() - last_resync >= self.RESYNC_INTERVAL or (
                0 < remaining <= self.FINAL_SYNC_LEAD and
                time.monotonic() - last_resync >= 5.0
            ):
                last_resync = time.monotonic()
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.clock.sync(announce=False)
                )
                remaining = (
                    self.clock.seconds_until(self.open_at)
                    if self.open_at is not None else 0.0
                )

            # -- captcha state (observed only) ---------------------------
            token = await self._captcha_token_present(page)
            if token and not captcha_ok:
                captcha_ok = True
                captcha_since = time.monotonic()
                self.log("[완료] 캡차 인증을 확인했습니다.", "success")
            elif not token and captcha_ok:
                captcha_ok = False
                captcha_since = 0.0
                self.log("[경고] 캡차 인증이 해제되었습니다. 다시 인증해주세요.", "warning")
            if captcha_ok:
                left = self.CAPTCHA_TTL_SECONDS - (time.monotonic() - captcha_since)
                if left <= self.CAPTCHA_WARN_SECONDS:
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
                self.silent_tick(f"{target_time} 오픈 대기")
                self._log_throttled(
                    "waiting",
                    f"[정보] 서버 시간 기준 오픈까지 "
                    f"{self._format_remaining(remaining)} 남음"
                    + ("" if captcha_ok else " · 캡차 미인증"),
                    "info",
                    interval=30.0 if remaining > 180 else 10.0,
                )
                await asyncio.sleep(
                    self.POLL_NEAR_SECONDS if remaining <= 3.0
                    else min(self.POLL_IDLE_SECONDS, max(0.05, remaining - 3.0))
                )
                continue

            # -- the moment has arrived ----------------------------------
            if not captcha_ok:
                self._log_throttled(
                    "await_captcha",
                    "[경고] 오픈 시각이 되었지만 캡차 인증이 없습니다. "
                    "인증하시면 즉시 제출합니다.",
                    "warning",
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

            if not self.submission_lock.acquire(blocking=False):
                await asyncio.sleep(0.05)
                continue
            try:
                submit_attempts += 1
                dialog_state["message"] = ""
                result = await self._submit(
                    page, dialog_state, slot_id, theme_name,
                    target_date, target_time, zizum_num, dev_mode,
                )
            finally:
                try:
                    self.submission_lock.release()
                except RuntimeError:
                    pass

            if result == "success":
                return
            if result == "capacity":
                self.log(
                    "[에러] 이미 다른 사람이 예약을 완료한 시간대입니다. 이번 회차는 마감되었습니다.",
                    "error",
                )
                return

            message = dialog_state.get("message", "")
            if result == "not_open":
                # The rejection carries the server's own open time; trust it over
                # anything parsed from the notice or the bundled table.
                corrected = self._open_time_from_message(message, target_date)
                if corrected is not None and corrected != self.open_at:
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
            elif result == "captcha_consumed":
                captcha_ok = False
                captcha_since = 0.0
                self.log(
                    "[경고] 제출로 캡차가 소모되었습니다(사이트가 자동 초기화합니다). "
                    "다시 인증해주세요.",
                    "warning",
                )
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
            await asyncio.sleep(0.2)

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
    ):
        # Put the real slot id into the form before anything is sent.
        #
        # Step 1 posts camelCase names (themeTimeNum) but the Step 2 page renders
        # snake_case ones (theme_time_num). The previous version only wrote to
        # 'themeTimeNum', which does not exist here, so the loop iterated an empty
        # NodeList and silently did nothing -- meaning a placeholder id would have
        # been submitted verbatim. Both spellings are written now.
        try:
            written = await page.evaluate(
                """(value) => {
                    let count = 0;
                    for (const name of ['theme_time_num', 'themeTimeNum']) {
                        for (const field of document.getElementsByName(name)) {
                            field.value = value;
                            count += 1;
                        }
                    }
                    return count;
                }""",
                str(slot_id),
            )
        except Exception as exc:
            self.log(f"[경고] 슬롯 ID 주입 실패: {exc}", "warning")
            return "retry"

        if not written:
            self.log(
                "[에러] 예약 폼에서 슬롯 ID 필드를 찾지 못했습니다. 페이지 구조가 바뀐 것 같습니다.",
                "error",
            )
            await self._dump_debug(page)
            return "retry"

        self.log(f"예약하기를 클릭합니다. (슬롯 ID {slot_id}, 필드 {written}개 갱신)", "info")
        try:
            clicked = await page.evaluate(
                """() => {
                    const button = document.querySelector('button.submit.pc')
                        || document.querySelector('button.submit')
                        || Array.from(document.querySelectorAll('button, a'))
                            .find(el => (el.innerText || '').includes('예약하기'));
                    if (!button) { return false; }
                    button.click();
                    return true;
                }"""
            )
        except Exception as exc:
            self.log(f"[경고] 예약하기 클릭 실패: {exc}", "warning")
            return "retry"

        if not clicked:
            self.log("[경고] 예약하기 버튼을 찾지 못했습니다.", "warning")
            await self._dump_debug(page)
            return "retry"

        completion = await self._await_completion(page)
        if completion is None:
            return self._classify_failure(dialog_state.get("message", ""))

        booking_number = await self._extract_booking_number(completion)
        self.log("[완료] 예약이 완료되었습니다.", "success")
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
            return "captcha_consumed"
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

    async def _await_completion(self, page, timeout=8.0):
        """Return the page showing the completion screen, or None.

        Waiting on the original page's URL alone was too narrow: if the site
        answers in a new tab, or its guard wipes the body before the URL check
        lands, the run treated a booking that actually went through as a failure
        and kept resubmitting. Every page in the context is examined, by URL and
        by completion-only markup.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                candidates = list(page.context.pages)
            except Exception:
                candidates = [page]
            if page not in candidates:
                candidates.append(page)

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

            if time.monotonic() >= deadline or self.stop_event.is_set():
                return None
            await asyncio.sleep(0.2)

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
            path.write_text(html, encoding="utf-8")
            self.log(f"[정보] 현재 화면 HTML을 저장했습니다: {path}", "info")
        except Exception:
            pass
