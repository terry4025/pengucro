import asyncio
import pathlib
import threading
import time
from datetime import datetime

import pytest

from engines.keyescape_engine import (
    DEVTOOLS_BLOCK_MARKER,
    DEVTOOLS_GUARD_SCRIPT,
    FALLBACK_SITEKEY,
    KST,
    KeyescapeEngine,
)
from engines.keyescape_coordination import SharedServerClock, SharedSlotLookup
from engines.yescaptcha_client import DEFAULT_SOFT_ID
from pengucro.storage import save_json


def make_engine():
    return KeyescapeEngine(lambda *_args: None)


class FakePage:
    """Minimal stand-in for a Playwright page.

    ``dom`` maps a probe source fragment onto the value ``evaluate`` should
    return, which is enough to exercise the completion and block detection
    without a browser.
    """

    def __init__(self, url="", body_text="", evaluate_map=None, context=None):
        self.url = url
        self.body_text = body_text
        self.evaluate_map = evaluate_map or {}
        self.context = context
        self.content_set = []
        self.handlers = {}

    async def evaluate(self, script, *args):
        for fragment, value in self.evaluate_map.items():
            if fragment in script:
                return value
        if "innerText" in script and args:
            return args[0] in self.body_text
        if "innerText" in script:
            return self.body_text
        return None

    async def inner_text(self, _selector, timeout=None):
        return self.body_text

    async def set_content(self, html):
        self.content_set.append(html)

    async def wait_for_load_state(self, _state):
        return None

    def on(self, event, handler):
        self.handlers[event] = handler


class FakeContext:
    def __init__(self, pages=None):
        self.pages = pages or []
        self.init_scripts = []
        self.routes = []

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))


class FakeRequest:
    def __init__(self, post_data="t=ins_rev", method="POST"):
        self.post_data = post_data
        self.method = method


class FakeResponse:
    def __init__(self, payload, *, post_data="t=ins_rev", url=None):
        self.url = url or "https://www.keyescape.com/controller/run_proc.php"
        self.request = FakeRequest(post_data=post_data)
        self.payload = payload

    async def json(self):
        return self.payload


class FakeDialog:
    def __init__(self, message):
        self.message = message
        self.accepted = False

    async def accept(self):
        self.accepted = True


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- guard script


def test_guard_script_stubs_detector_and_seals_keydown():
    assert "devtoolsDetector" in DEVTOOLS_GUARD_SCRIPT
    assert "onkeydown" in DEVTOOLS_GUARD_SCRIPT
    # A read-only property would make the real library's strict-mode assignment
    # throw; the guard has to swallow writes instead.
    assert "writable: false" not in DEVTOOLS_GUARD_SCRIPT
    assert "set() {}" in DEVTOOLS_GUARD_SCRIPT


def test_harden_context_registers_init_script_and_route():
    engine = make_engine()
    context = FakeContext()
    run(engine._harden_context(context))
    assert context.init_scripts == [DEVTOOLS_GUARD_SCRIPT]
    assert len(context.routes) == 1
    assert context.routes[0][0].search(
        "https://cdnjs.cloudflare.com/ajax/libs/devtools-detector/2.0.22/devtools-detector.min.js"
    )


def test_harden_context_survives_unsupported_context():
    engine = make_engine()

    class Broken(FakeContext):
        async def add_init_script(self, script):
            raise RuntimeError("nope")

    logs = []
    engine.log = lambda message, *_a, **_k: logs.append(message)
    run(engine._harden_context(Broken()))
    assert any("우회 스크립트 등록 실패" in message for message in logs)


# ------------------------------------------------------------ block detection


def test_is_blocked_detects_wiped_body():
    engine = make_engine()
    blocked = FakePage(body_text=f"{DEVTOOLS_BLOCK_MARKER}되어 있습니다.")
    normal = FakePage(body_text="예약 정보 입력")
    assert run(engine._is_blocked(blocked)) is True
    assert run(engine._is_blocked(normal)) is False


def test_restore_step_two_without_snapshot_is_a_noop():
    engine = make_engine()
    assert run(engine._restore_step_two(FakePage(), {})) is False


def test_restore_step_two_reposts_the_saved_form():
    engine = make_engine()
    engine._step_two_html = "<html><form id='f'></form></html>"
    engine._fill_form = lambda *_a, **_k: asyncio.sleep(0)
    page = FakePage()
    assert run(engine._restore_step_two(page, {"name": "테스트"})) is True
    assert page.content_set == [engine._step_two_html]


# ------------------------------------------------------- completion detection


def test_prepare_page_captures_booking_response_before_navigation():
    engine = make_engine()
    page = FakePage()
    state = engine._new_submission_state()
    run(engine._prepare_page(page, state))

    run(page.handlers["response"](FakeResponse({
        "status": True,
        "msg": "입금확인이 되면 예약확정 문자가 발송됩니다.",
        "data": {"num": "95882", "ck_code": "abc"},
    })))

    assert state["submission_status"] == "success"
    assert state["booking_number"] == "95882"
    assert state["ck_code"] == "abc"
    assert engine._page_success_event.is_set()


def test_prepare_page_captures_success_without_post_data_or_check_code():
    engine = make_engine()
    page = FakePage()
    state = engine._new_submission_state()
    run(engine._prepare_page(page, state))

    run(page.handlers["response"](FakeResponse({
        "status": True,
        "msg": "입금확인이 되면 예약확정 문자가 발송됩니다.",
        "data": {"num": "95883"},
    }, post_data="")))

    assert state["submission_status"] == "success"
    assert state["booking_number"] == "95883"


def test_prepare_page_ignores_other_run_proc_actions():
    engine = make_engine()
    page = FakePage()
    state = engine._new_submission_state()
    run(engine._prepare_page(page, state))

    run(page.handlers["response"](FakeResponse(
        {"status": True, "data": [{"num": "slot-row"}]},
        post_data="t=get_theme_time",
    )))

    assert state["submission_status"] == ""
    assert state["booking_number"] == ""


def test_success_dialog_is_recorded_as_booking_success():
    engine = make_engine()
    page = FakePage()
    state = engine._new_submission_state()
    run(engine._prepare_page(page, state))
    dialog = FakeDialog("입금확인이 되면 예약확정 문자가 발송됩니다.")

    run(page.handlers["dialog"](dialog))

    assert dialog.accepted is True
    assert state["submission_status"] == "success"
    assert engine._page_success_event.is_set()


def test_await_completion_matches_reservation3_url():
    engine = make_engine()
    target = FakePage(url="https://www.keyescape.com/reservation3.php")
    context = FakeContext([target])
    target.context = context
    assert run(engine._await_completion(target, timeout=0.1)) is target


def test_await_completion_finds_completion_in_another_tab():
    engine = make_engine()
    step_two = FakePage(url="https://www.keyescape.com/reservation2.php")
    popup = FakePage(url="https://www.keyescape.com/reservation3.php")
    context = FakeContext([step_two, popup])
    step_two.context = popup.context = context
    assert run(engine._await_completion(step_two, timeout=0.1)) is popup


def test_hot_standby_completion_does_not_claim_a_sibling_page():
    engine = make_engine()
    own_page = FakePage(
        url="https://www.keyescape.com/reservation2.php",
        evaluate_map={"resrv_result2": False},
    )
    sibling_success = FakePage(url="https://www.keyescape.com/reservation3.php")
    context = FakeContext([own_page, sibling_success])
    own_page.context = sibling_success.context = context

    assert run(engine._await_completion(
        own_page,
        timeout=0.05,
        include_context_pages=False,
    )) is None


def test_await_completion_accepts_completion_markup_without_url():
    engine = make_engine()
    page = FakePage(
        url="about:blank",
        evaluate_map={"resrv_result2": True},
    )
    page.context = FakeContext([page])
    assert run(engine._await_completion(page, timeout=0.1)) is page


def test_await_completion_ignores_step_two_text():
    """'예약 완료' appears on step 2 as a step label, so text must not count."""
    engine = make_engine()
    page = FakePage(
        url="https://www.keyescape.com/reservation2.php",
        body_text="STEP 3. 예약 완료 · 예약완료시 예약확인 문자가 발송이 됩니다.",
        evaluate_map={"resrv_result2": False},
    )
    page.context = FakeContext([page])
    assert run(engine._await_completion(page, timeout=0.1)) is None


def test_await_completion_stops_when_asked_to_stop():
    engine = make_engine()
    engine.stop_event = threading.Event()
    engine.stop_event.set()
    page = FakePage(url="about:blank", evaluate_map={"resrv_result2": False})
    page.context = FakeContext([page])
    assert run(engine._await_completion(page, timeout=5.0)) is None


def test_inflight_completion_can_finish_after_another_page_succeeds():
    class DelayedCompletionPage(FakePage):
        def __init__(self):
            super().__init__(url="https://www.keyescape.com/reservation2.php")
            self.probes = 0

        async def evaluate(self, script, *args):
            if "resrv_result2" in script:
                self.probes += 1
                return self.probes >= 2
            return await super().evaluate(script, *args)

    engine = make_engine()
    engine.stop_event.set()
    page = DelayedCompletionPage()
    page.context = FakeContext([page])

    assert run(engine._await_completion(
        page,
        timeout=0.5,
        include_context_pages=False,
        finish_inflight=True,
    )) is page


def test_booking_response_completes_even_if_driver_never_reaches_result_page():
    engine = make_engine()
    page = FakePage(
        url="https://www.keyescape.com/reservation2.php",
        evaluate_map={"resrv_result2": False},
    )
    page.context = FakeContext([page])
    state = engine._new_submission_state()
    state.update({"submission_status": "success", "booking_number": "95882"})

    assert run(engine._await_completion(
        page,
        timeout=5.0,
        submission_state=state,
    )) is page


# --------------------------------------------------------- booking number


def test_booking_number_from_completion_dom():
    engine = make_engine()
    page = FakePage(evaluate_map={"resrv_info_list": "15490"})
    assert run(engine._extract_booking_number(page)) == "15490"


def test_booking_number_falls_back_to_body_text():
    engine = make_engine()
    page = FakePage(
        body_text="예약번호 15490\n인원 2명",
        evaluate_map={"resrv_info_list": ""},
    )
    assert run(engine._extract_booking_number(page)) == "15490"


def test_booking_number_waits_for_late_ajax_response():
    async def scenario():
        engine = make_engine()
        page = FakePage(evaluate_map={"resrv_info_list": ""})
        state = engine._new_submission_state()

        async def publish_number():
            await asyncio.sleep(0.03)
            state["booking_number"] = "95884"

        task = asyncio.create_task(publish_number())
        number = await engine._resolve_booking_number(page, state, timeout=0.2)
        await task
        return number

    assert run(scenario()) == "95884"


def test_driver_connection_error_is_recognized():
    assert KeyescapeEngine._is_browser_connection_error(
        RuntimeError("Page.evaluate: Connection closed while reading from the driver")
    )


# ------------------------------------------------------- failure classification


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("[예약불가] 예약이 이미 완료되었습니다.", "capacity"),
        ("[에러] 잘못된 접근입니다.", "invalid_request"),
        ("예약가능시간이 아닙니다. 예약오픈시간 : 11:00", "not_open"),
        ("", "retry"),
    ],
)
def test_classify_failure(message, expected):
    assert KeyescapeEngine._classify_failure(message) == expected


def test_submit_refuses_blank_or_placeholder_slot_id():
    engine = make_engine()
    page = RecordingPage()
    for slot_id in ("", engine.PLACEHOLDER_SLOT_ID):
        assert run(engine._submit(
            page, {"message": ""}, slot_id, "테마",
            "2026-08-15", "09:50", "22", False,
        )) == "slot_not_ready"
    assert page.calls == []


def test_submit_updates_slot_and_clicks_in_one_playwright_call():
    engine = make_engine()
    page = RecordingPage(results={
        "const slotId": {
            "written": 1,
            "captchaReady": True,
            "buttonFound": True,
            "clicked": True,
        }
    })

    async def no_completion(*_args, **_kwargs):
        return None

    engine._await_completion = no_completion
    result = run(engine._submit(
        page, {"message": ""}, "2301", "테마",
        "2026-08-15", "09:50", "22", False,
    ))

    assert result == "retry"
    assert len(page.calls) == 1
    assert page.calls[0][1][0]["slotId"] == "2301"
    assert "button.click()" in page.calls[0][0]


def test_manual_captcha_age_uses_safety_ttl():
    engine = make_engine()
    now = time.monotonic()
    assert engine._manual_captcha_expired(now - engine.CAPTCHA_TTL_SECONDS + 1) is False
    assert engine._manual_captcha_expired(now - engine.CAPTCHA_TTL_SECONDS - 1) is True


def test_live_slot_resolution_uses_target_date_id_and_shares_result():
    engine = make_engine()
    calls = []

    async def fetch(date_str, branch, theme):
        calls.append((date_str, branch, theme))
        return [{"num": "2301", "hh": "9", "mm": "50", "enable": "Y"}]

    engine._fetch_slots = fetch
    engine._live_slot_state = {
        "slot_id": "", "status": "pending", "last_probe": 0.0,
        "lock": asyncio.Lock(),
    }

    async def scenario():
        first = await engine._resolve_live_slot(
            "2026-08-15", "09:50", "22", "65", ""
        )
        second = await engine._resolve_live_slot(
            "2026-08-15", "09:50", "22", "65", ""
        )
        return first, second

    first, second = run(scenario())
    assert first == ("2301", "ready")
    assert second == first
    assert calls == [("2026-08-15", "22", "65")]


def test_timing_parameters_adapt_to_observed_slot_read_rtt():
    hedge, retry, read_lead = KeyescapeEngine._timing_parameters([
        {"read_rtt_ms": 80},
        {"read_rtt_ms": 100},
        {"read_rtt_ms": 120},
    ])

    assert hedge == pytest.approx(0.035)
    assert retry == pytest.approx(0.1)
    assert read_lead == pytest.approx(0.05)


def test_cold_start_sends_primary_well_before_opening_congestion():
    hedge, retry, read_lead = KeyescapeEngine._cold_start_timing_parameters(0.04)

    assert hedge == pytest.approx(KeyescapeEngine.SLOT_HEDGE_MIN_SECONDS)
    assert retry == pytest.approx(KeyescapeEngine.SLOT_RETRY_MIN_SECONDS)
    assert read_lead == pytest.approx(0.45)


def test_cold_start_lead_adapts_to_slow_preflight_but_stays_bounded():
    _hedge, _retry, read_lead = KeyescapeEngine._cold_start_timing_parameters(1.2)
    assert read_lead == pytest.approx(0.6)

    _hedge, _retry, capped = KeyescapeEngine._cold_start_timing_parameters(2.0)
    assert capped == pytest.approx(KeyescapeEngine.SLOT_READ_LEAD_MAX_SECONDS)


def test_first_live_slot_read_uses_faster_valid_hedge():
    engine = make_engine()
    secondary_session = object()
    engine._slot_hedge_session = secondary_session
    calls = []

    async def fetch(session, date_str, branch, theme, end_day=0):
        calls.append(session)
        if session is engine._session:
            await asyncio.sleep(0.05)
            return [{"num": "9999", "hh": "9", "mm": "50", "enable": "Y"}]
        await asyncio.sleep(0.001)
        return [{"num": "2301", "hh": "9", "mm": "50", "enable": "Y"}]

    engine._fetch_slots_with_session = fetch
    engine._live_slot_state = {
        "hedges_remaining": 1,
        "hedge_delay": 0.001,
        "last_rtt": 0.0,
    }

    slots = run(engine._fetch_live_slots("2026-08-15", "22", "65", "09:50"))

    assert engine._match_slot(slots, "09:50") == ("2301", True)
    assert calls == [engine._session, secondary_session]
    assert engine._live_slot_state["hedges_remaining"] == 0
    assert engine._live_slot_state["last_rtt"] > 0


def test_fast_primary_live_slot_read_does_not_send_hedge():
    engine = make_engine()
    calls = []

    async def fetch(session, date_str, branch, theme, end_day=0):
        calls.append(session)
        return [{"num": "2301", "hh": "9", "mm": "50", "enable": "Y"}]

    engine._fetch_slots_with_session = fetch
    engine._live_slot_state = {
        "hedges_remaining": 1,
        "hedge_delay": 0.001,
        "last_rtt": 0.0,
    }

    slots = run(engine._fetch_live_slots("2026-08-15", "22", "65", "09:50"))

    assert engine._match_slot(slots, "09:50") == ("2301", True)
    assert calls == [engine._session]


def test_first_closed_response_waits_for_second_open_response():
    engine = make_engine()
    secondary_session = object()
    engine._slot_hedge_session = secondary_session
    calls = []

    async def fetch(session, date_str, branch, theme, end_day=0):
        calls.append(session)
        if session is engine._session:
            return [{"num": "2301", "hh": "9", "mm": "50", "enable": "N"}]
        return [{"num": "2301", "hh": "9", "mm": "50", "enable": "Y"}]

    engine._fetch_slots_with_session = fetch
    engine._live_slot_state = {
        "hedges_remaining": 1,
        "hedge_delay": 0.001,
        "last_rtt": 0.0,
        "closed_observations": 0,
    }

    slots = run(engine._fetch_live_slots("2026-08-15", "22", "65", "09:50"))

    assert engine._match_slot(slots, "09:50") == ("2301", True)
    assert calls == [engine._session, secondary_session]
    assert engine._live_slot_state["closed_observations"] == 1


def test_closed_slot_requires_two_independent_boundary_responses():
    engine = make_engine()
    secondary_session = object()
    engine._slot_hedge_session = secondary_session

    async def fetch(session, date_str, branch, theme, end_day=0):
        return [{"num": "2301", "hh": "9", "mm": "50", "enable": "N"}]

    engine._fetch_slots_with_session = fetch
    engine._live_slot_state = {
        "hedges_remaining": 1,
        "hedge_delay": 0.001,
        "last_rtt": 0.0,
        "closed_observations": 0,
    }

    slots = run(engine._fetch_live_slots("2026-08-15", "22", "65", "09:50"))

    assert engine._match_slot(slots, "09:50") == ("2301", False)
    assert engine._live_slot_state["closed_observations"] == 2


def test_single_retry_cannot_declare_capacity_without_second_observation():
    engine = make_engine()

    async def fetch(session, date_str, branch, theme, end_day=0):
        return [{"num": "2301", "hh": "9", "mm": "50", "enable": "N"}]

    engine._fetch_slots_with_session = fetch
    engine._live_slot_state = {
        "hedges_remaining": 0,
        "last_rtt": 0.0,
        "closed_observations": 0,
    }

    first = run(engine._fetch_live_slots("2026-08-15", "22", "65", "09:50"))
    second = run(engine._fetch_live_slots("2026-08-15", "22", "65", "09:50"))

    assert first == []
    assert engine._match_slot(second, "09:50") == ("2301", False)
    assert engine._live_slot_state["closed_observations"] == 2


def test_quiet_wait_scopes_high_resolution_timer():
    engine = make_engine()
    engine.open_at = time.time() + 0.01
    engine._live_slot_state = {"read_lead": 0.001}
    events = []
    engine._begin_high_resolution_timer = lambda: events.append("begin") or True
    engine._end_high_resolution_timer = lambda: events.append("end")

    run(engine._wait_for_open_quiet())

    assert events == ["begin", "end"]


def _template_slots(first="2827", second="2828", gubun=""):
    return [
        {"num": first, "hh": "19", "mm": "50", "enable": "Y", "gubun": gubun},
        {"num": second, "hh": "20", "mm": "45", "enable": "Y", "gubun": gubun},
    ]


def test_trusted_slot_requires_two_distinct_matching_published_dates(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    engine = make_engine()

    engine._remember_slot_template("2026-08-12", "23", "69", _template_slots())
    assert engine._trusted_slot_from_cache(
        "2026-08-18", "20:45", "23", "69"
    ) == ("", ())

    engine._remember_slot_template("2026-08-13", "23", "69", _template_slots())
    slot_id, sources = engine._trusted_slot_from_cache(
        "2026-08-18", "20:45", "23", "69"
    )

    assert slot_id == "2828"
    assert sources == ("2026-08-13", "2026-08-12")


def test_trusted_slot_is_disabled_when_latest_schedule_signature_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    engine = make_engine()
    engine._remember_slot_template("2026-08-12", "23", "69", _template_slots())
    engine._remember_slot_template(
        "2026-08-13", "23", "69", _template_slots(second="9998")
    )

    assert engine._trusted_slot_from_cache(
        "2026-08-18", "20:45", "23", "69"
    ) == ("", ())


def test_fresh_weekend_schedule_group_can_arm_only_one_fast_page(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    engine = make_engine()
    engine._remember_slot_template(
        "2026-08-15", "23", "69", _template_slots(gubun="C")
    )

    assert engine._trusted_slot_from_cache(
        "2026-08-22", "20:45", "23", "69"
    ) == ("2828", ("2026-08-15",))


def test_trusted_slot_reads_previous_release_reservation_php_cache_key(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    engine = make_engine()
    signature, mapping = engine._slot_template_payload(
        _template_slots(gubun="C")
    )
    save_json(engine.SLOT_TEMPLATE_FILE, {
        "version": 1,
        "entries": {
            "https://www.keyescape.com/reservation.php|23|69": [{
                "date": "2026-08-15",
                "weekday": 5,
                "group": "weekday_5",
                "gubun": "C",
                "signature": signature,
                "slots": mapping,
            }],
        },
    })

    assert engine._trusted_slot_from_cache(
        "2026-08-22", "20:45", "23", "69"
    ) == ("2828", ("2026-08-15",))


def test_weekend_single_schedule_rejects_wrong_server_group(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    engine = make_engine()
    engine._remember_slot_template(
        "2026-08-15", "23", "69", _template_slots(gubun="B")
    )

    assert engine._trusted_slot_from_cache(
        "2026-08-22", "20:45", "23", "69"
    ) == ("", ())


def test_prime_trusted_slot_refreshes_all_published_dates_for_local_history(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    engine = make_engine()
    server_now = datetime(2026, 8, 22, 10, 51, tzinfo=KST).timestamp()
    engine.clock = type("Clock", (), {"now": staticmethod(lambda: server_now)})()
    requested = []

    async def fetch_slots(target_date, _zizum_num, _theme_num):
        requested.append(target_date)
        weekday = datetime.strptime(target_date, "%Y-%m-%d").weekday()
        gubun = ("A", "A", "A", "A", "B", "C", "D")[weekday]
        return _template_slots(gubun=gubun)

    engine._fetch_slots = fetch_slots

    slot_id, sources = run(engine._prime_trusted_slot_template(
        "2026-08-29", "20:45", "23", "69", 8
    ))

    assert slot_id == "2828"
    assert sources == ("2026-08-22",)
    assert requested == [
        "2026-08-28", "2026-08-27", "2026-08-26", "2026-08-25",
        "2026-08-24", "2026-08-23", "2026-08-22",
    ]


def test_only_first_standby_page_receives_trusted_fast_slot():
    engine = make_engine()
    engine._trusted_slot_id = "2828"
    engine._trusted_slot_sources = ("2026-08-12", "2026-08-13")

    first = engine._make_page_worker(1)
    second = engine._make_page_worker(2)

    assert first._trusted_slot_id == "2828"
    assert first._trusted_slot_sources == engine._trusted_slot_sources
    assert second._trusted_slot_id == ""


def test_one_page_trusted_run_automatically_adds_live_fallback_page():
    engine = make_engine()
    engine._page_count = 1
    engine._trusted_slot_id = "2828"

    assert engine._ensure_parallel_live_fallback(already_open=False) is True
    assert engine._page_count == 2
    assert engine._ensure_parallel_live_fallback(already_open=False) is False


def test_trusted_first_page_and_live_fallback_page_use_separate_slot_paths():
    async def scenario():
        coordinator = make_engine()
        coordinator._page_count = 2
        coordinator._trusted_slot_id = "2828"
        coordinator._trusted_slot_sources = ("2026-08-12", "2026-08-13")
        coordinator._live_slot_state = {
            "slot_id": "2301",
            "status": "ready",
            "last_probe": 0.0,
            "lock": asyncio.Lock(),
        }
        first = coordinator._make_page_worker(1)
        second = coordinator._make_page_worker(2)
        submitted = {}

        def configure(worker):
            worker.open_at = None
            worker._is_blocked = lambda _page: asyncio.sleep(0, result=False)
            worker._captcha_token_value = lambda _page: asyncio.sleep(
                0, result="manual-token"
            )
            worker._ensure_yescaptcha_token = lambda *_args: None

            async def submit(_page, _state, slot_id, *_args, **_kwargs):
                submitted[worker._page_index] = slot_id
                return "success"

            worker._submit = submit
            return worker._watch_and_submit(
                RecordingPage(),
                {"message": ""},
                {"devMode": False, "yescaptcha_enabled": False},
                "2026-08-18",
                "20:45",
                "23",
                "69",
                "투투 어드벤쳐",
                "9999",
            )

        await asyncio.gather(configure(first), configure(second))
        return submitted

    assert run(scenario()) == {1: "2828", 2: "2301"}


def test_trusted_fire_wait_includes_server_clock_precision_margin():
    engine = make_engine()
    engine.open_at = 1000.0
    values = iter((999.9, 1000.07))
    last = [1000.07]

    def now():
        try:
            last[0] = next(values)
        except StopIteration:
            pass
        return last[0]

    engine.clock = type(
        "Clock", (), {"last_precision": 0.06, "now": staticmethod(now)}
    )()
    events = []
    engine._begin_high_resolution_timer = lambda: events.append("begin") or True
    engine._end_high_resolution_timer = lambda: events.append("end")

    run(engine._wait_for_trusted_fire())

    assert events == ["begin", "end"]
    assert last[0] >= 1000.065


def test_shared_slot_lookup_publishes_only_public_timetable_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    owner = SharedSlotLookup("site|23|69|2026-08-18", time.time())
    assert owner.prepare() is True
    owner.mark_started()
    rows = _template_slots()
    owner.publish(rows)

    follower = SharedSlotLookup("site|23|69|2026-08-18", owner.open_at)
    follower.owner = False

    assert follower.wait_for_result(0.1) == rows


def test_shared_server_clock_reuses_one_local_measurement(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))

    class Clock:
        def __init__(self):
            self.sync_calls = 0
            self.applied = False
            self.shared_announced = False

        def apply_snapshot(self, value, max_age):
            del max_age
            self.applied = bool(value.get("mapping"))
            return self.applied

        def sync(self, announce=False):
            del announce
            self.sync_calls += 1
            return True

        @staticmethod
        def snapshot():
            return {
                "mapping": 100.0,
                "precision": 0.03,
                "captured_monotonic": time.monotonic(),
            }

        def announce_sync(self, shared=False):
            self.shared_announced = shared

    first_clock = Clock()
    assert SharedServerClock("site").sync(first_clock) is True
    assert first_clock.sync_calls == 1

    second_clock = Clock()
    assert SharedServerClock("site").sync(second_clock, announce=True) is True
    assert second_clock.sync_calls == 0
    assert second_clock.applied is True
    assert second_clock.shared_announced is True


def test_coordinated_slot_read_uses_other_process_result_without_network():
    engine = make_engine()
    rows = _template_slots()
    calls = []

    class Share:
        owner = False

        def wait_for_result(self, _timeout):
            return rows

    async def network(*_args):
        calls.append(True)
        return []

    engine._slot_share = Share()
    engine._live_slot_state = {}
    engine._fetch_live_slots = network

    result = run(engine._fetch_coordinated_live_slots(
        "2026-08-18", "23", "69", "20:45"
    ))

    assert result == rows
    assert calls == []


def test_browser_submission_connection_prewarm_uses_same_origin_head():
    page = RecordingPage(results={"pg_prewarm": True})
    engine = make_engine()

    assert run(engine._prewarm_browser_connection(page)) is True
    assert any("method: 'HEAD'" in script for script, _args in page.calls)



# ------------------------------------------------------------------- captcha


class RecordingPage:
    """A page that records every evaluate() call with its arguments."""

    def __init__(self, results=None, frames=None):
        self.results = results or {}
        self.calls = []
        self.frames = frames or []

    async def evaluate(self, script, *args):
        self.calls.append((script, args))
        for fragment, value in self.results.items():
            if fragment in script:
                return value() if callable(value) else value
        return None

    async def bring_to_front(self):
        return None


class FakeLocator:
    def __init__(self, frame):
        self.frame = frame

    async def click(self, timeout=None):
        self.frame.clicks += 1


class FakeFrame:
    def __init__(self, url):
        self.url = url
        self.clicks = 0

    def locator(self, _selector):
        return FakeLocator(self)


def anchor_page(**kwargs):
    frame = FakeFrame("https://www.google.com/recaptcha/api2/anchor?k=abc")
    page = RecordingPage(frames=[frame], **kwargs)
    return page, frame


# -- settings plumbing ---------------------------------------------------


def test_read_yescaptcha_settings_from_engine_payload():
    payload = {
        "yescaptcha_enabled": True,
        "yescaptcha_client_key": "  key-123  ",
        "yescaptcha_soft_id": "999",
    }
    assert KeyescapeEngine.read_yescaptcha_settings(payload) == (True, "key-123", "999")


def test_read_yescaptcha_settings_defaults_soft_id():
    payload = {"yescaptcha_enabled": True, "yescaptcha_client_key": "k"}
    enabled, key, soft = KeyescapeEngine.read_yescaptcha_settings(payload)
    assert (enabled, key) == (True, "k")
    assert soft == DEFAULT_SOFT_ID


def test_read_yescaptcha_settings_absent_keys_disable_it():
    # Payloads built before the keys existed must not look "enabled".
    assert KeyescapeEngine.read_yescaptcha_settings({"branch": "3"}) == (
        False, "", DEFAULT_SOFT_ID,
    )


@pytest.mark.parametrize("value", [False, None, "", "false", "FALSE", "off", "0"])
def test_read_yescaptcha_settings_keeps_false_like_values_off(value):
    payload = {
        "yescaptcha_enabled": value,
        "yescaptcha_client_key": "must-not-run",
    }
    assert KeyescapeEngine.read_yescaptcha_settings(payload)[0] is False


def test_read_yescaptcha_settings_accepts_an_object():
    class Request:
        yescaptcha_enabled = True
        yescaptcha_client_key = "obj-key"
        yescaptcha_soft_id = "1"

    assert KeyescapeEngine.read_yescaptcha_settings(Request()) == (True, "obj-key", "1")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), ("true", True), ("on", True), (False, False), ("false", False)],
)
def test_read_yescaptcha_test_mode(value, expected):
    payload = {"yescaptcha_test_mode": value}
    assert KeyescapeEngine.read_yescaptcha_test_mode(payload) is expected


def test_start_reservation_clamps_to_three_pages_on_one_coordinator():
    import engines.base_engine as base_engine

    engine = make_engine()
    seen = {}

    def fake_start(_self, reservation_data, num_threads, is_async=False):
        seen["num_threads"] = num_threads
        seen["is_async"] = is_async

    original = base_engine.BaseEngine.start_reservation
    base_engine.BaseEngine.start_reservation = fake_start
    try:
        engine.start_reservation(
            {"yescaptcha_enabled": True, "yescaptcha_client_key": "k"}, num_threads=30
        )
    finally:
        base_engine.BaseEngine.start_reservation = original

    assert engine._page_count == engine.MAX_STANDBY_PAGES
    assert seen == {"num_threads": 1, "is_async": False}


def test_page_workers_share_stop_gate_but_keep_submission_and_captcha_independent():
    engine = make_engine()
    engine._page_count = 2
    first = engine._make_page_worker(1)
    second = engine._make_page_worker(2)

    assert first.stop_event is second.stop_event is engine.stop_event
    assert first._page_success_event is second._page_success_event
    assert first._page_success_event is engine._page_success_event
    assert first.submission_lock is not second.submission_lock
    assert first._page_count == second._page_count == 2
    first._yc_token = "first-token"
    assert second._yc_token == ""


def test_ready_standby_pages_enter_submit_concurrently():
    async def scenario():
        coordinator = make_engine()
        coordinator._page_count = 2
        first = coordinator._make_page_worker(1)
        second = coordinator._make_page_worker(2)
        # Even if a future refactor accidentally shares the base lock again, the
        # Keyescape hot-standby path must not use it to serialize submissions.
        first.submission_lock = second.submission_lock = coordinator.submission_lock

        entered = []
        both_entered = asyncio.Event()
        release = asyncio.Event()
        peak = 0
        in_flight = 0

        def configure(worker):
            async def not_blocked(_page):
                return False

            async def manual_token(_page):
                return "manual-token"

            async def submit(*_args, **_kwargs):
                nonlocal peak, in_flight
                entered.append(worker._page_index)
                in_flight += 1
                peak = max(peak, in_flight)
                if len(entered) == 2:
                    both_entered.set()
                    release.set()
                await asyncio.wait_for(release.wait(), timeout=0.5)
                in_flight -= 1
                return "capacity"

            worker.open_at = None
            worker._is_blocked = not_blocked
            worker._captcha_token_value = manual_token
            worker._ensure_yescaptcha_token = lambda *_args: None
            worker._submit = submit
            return worker._watch_and_submit(
                RecordingPage(),
                {"message": ""},
                {"devMode": False, "yescaptcha_enabled": False},
                "2026-08-08",
                "13:20",
                "14",
                "51",
                "머니머니패키지",
                "1892",
            )

        await asyncio.wait_for(
            asyncio.gather(
                configure(first),
                configure(second),
                both_entered.wait(),
            ),
            timeout=1.0,
        )
        return entered, peak

    entered, peak = run(scenario())
    assert set(entered) == {1, 2}
    assert peak == 2


def test_first_successful_page_becomes_the_only_winner():
    engine = make_engine()
    first = engine._make_page_worker(1)
    second = engine._make_page_worker(2)

    assert second.notify_success() is True
    assert engine._winner_page == 2
    assert engine.stop_event.is_set()
    assert first.notify_success() is False
    assert engine._winner_page == 2
    assert engine._page_success_event.is_set()


def test_capacity_after_sibling_success_is_logged_as_normal_completion():
    engine = make_engine()
    engine._page_count = 3
    engine._page_success_event.set()
    logs = []
    engine.log = lambda message, kind="info": logs.append((message, kind))

    assert run(engine._report_capacity_result()) == "sibling"
    assert any("다른 핫 스탠바이 페이지가 먼저 예약에 성공" in message for message, _ in logs)
    assert not any(kind == "error" for _, kind in logs)


def test_server_corrected_open_time_propagates_to_all_standby_pages():
    engine = make_engine()
    engine._page_workers = [
        engine._make_page_worker(1),
        engine._make_page_worker(2),
        engine._make_page_worker(3),
    ]

    engine._set_shared_open_at(1234.5)

    assert engine.open_at == 1234.5
    assert all(worker.open_at == 1234.5 for worker in engine._page_workers)


def test_yescaptcha_waiting_at_open_never_prints_manual_instruction():
    engine = make_engine()
    messages = []

    def record(message, _kind="info"):
        messages.append(message)
        if "오픈 시각 도달" in message:
            engine.stop_event.set()

    async def not_blocked(_page):
        return False

    async def no_widget_token(_page):
        return ""

    async def sitekey(_page):
        return "site-key"

    engine.log = record
    engine.open_at = None
    engine._is_blocked = not_blocked
    engine._captcha_token_value = no_widget_token
    engine._read_sitekey = sitekey
    engine._ensure_yescaptcha_token = lambda *_args: None

    run(engine._watch_and_submit(
        RecordingPage(),
        {"message": ""},
        {
            "devMode": False,
            "yescaptcha_enabled": True,
            "yescaptcha_client_key": "key",
        },
        "2026-08-08",
        "13:20",
        "14",
        "51",
        "머니머니패키지",
        "1892",
    ))

    assert any(
        "YesCaptcha" in message and "토큰 발급 대기" in message
        for message in messages
    )
    assert not any("인증하시면 즉시 제출" in message for message in messages)


# -- sitekey -------------------------------------------------------------


def test_read_sitekey_prefers_the_live_page():
    engine = make_engine()
    page = RecordingPage(results={"data-sitekey": "live-key"})
    assert run(engine._read_sitekey(page)) == "live-key"
    # Cached: a second call must not re-query the page.
    calls = len(page.calls)
    assert run(engine._read_sitekey(page)) == "live-key"
    assert len(page.calls) == calls


def test_read_sitekey_falls_back_when_the_page_has_none():
    engine = make_engine()
    page = RecordingPage(results={"data-sitekey": ""})
    assert run(engine._read_sitekey(page)) == FALLBACK_SITEKEY


def test_fallback_sitekey_matches_the_reference_page():
    reference = (
        pathlib.Path(__file__).resolve().parents[1]
        / "reference" / "keyescape" / "reservation2.html"
    )
    if not reference.exists():
        pytest.skip("reference page not bundled")
    assert FALLBACK_SITEKEY in reference.read_text(encoding="utf-8", errors="ignore")


# -- token injection -----------------------------------------------------


def test_write_token_passes_the_token_as_an_argument():
    """Never interpolated into the script source."""
    engine = make_engine()
    page = RecordingPage(results={"__pgCaptchaToken": 1})
    token = 'tok"en\\with\'quotes'
    assert run(engine._write_token(page, token)) == 1
    script, args = page.calls[-1]
    assert args == (token,)
    assert token not in script


def test_write_token_clear_only_restores_the_real_page_state():
    engine = make_engine()
    page = RecordingPage(results={"__pgCaptchaToken": 1})

    assert run(engine._write_token(page, "")) == 1
    assert page.calls[0][1] == ("",)
    assert len(page.calls) == 1


def test_write_token_reports_zero_when_the_field_is_missing():
    engine = make_engine()
    page = RecordingPage(results={"__pgCaptchaToken": 0})
    assert run(engine._write_token(page, "tok")) == 0


def test_yescaptcha_injection_is_a_strict_noop_when_off():
    engine = make_engine()
    engine._yc_enabled = False
    engine._yc_client_key = "configured-but-off"
    page = RecordingPage()

    assert run(engine._inject_yescaptcha_token(page, "tok")) is False
    assert page.calls == []


def test_yescaptcha_injection_requires_form_and_getter_to_match():
    engine = make_engine()
    engine._yc_enabled = True
    engine._yc_client_key = "key"
    ready = {
        "formFields": 1,
        "formMatches": True,
        "getterMatches": True,
    }
    page = RecordingPage(
        results={
            "__pgCaptchaToken": 1,
            "formMatches": ready,
            "__pg-yescaptcha-status": True,
        }
    )

    assert run(engine._inject_yescaptcha_token(page, "tok")) is True
    assert len(page.calls) == 2


def test_yescaptcha_injection_rejects_a_token_missing_from_formdata():
    engine = make_engine()
    engine._yc_enabled = True
    engine._yc_client_key = "key"
    page = RecordingPage(
        results={
            "__pgCaptchaToken": 1,
            "formMatches": {
                "formFields": 1,
                "formMatches": False,
                "getterMatches": True,
            },
        }
    )

    assert run(engine._inject_yescaptcha_token(page, "tok")) is False


def test_token_patch_restores_the_original_getter_when_cleared():
    script = KeyescapeEngine.TOKEN_PATCH_SCRIPT
    assert "patch.owner.getResponse = patch.original" in script
    assert "delete window.__pgCaptchaPatch" in script


def test_captcha_token_present_ignores_the_patched_getter():
    """Must read the textarea only.

    Consulting grecaptcha.getResponse() would report the injected token as a
    freshly solved widget and make expiry undetectable.
    """
    engine = make_engine()
    page = RecordingPage(results={"g-recaptcha-response": True})
    assert run(engine._captcha_token_present(page)) is True
    script = page.calls[-1][0]
    assert "getResponse" not in script


def test_injected_textarea_token_is_not_mistaken_for_manual_captcha():
    assert KeyescapeEngine._is_manual_widget_token("api-token", "api-token") is False
    assert KeyescapeEngine._is_manual_widget_token("manual-token", "api-token") is True
    assert KeyescapeEngine._is_manual_widget_token("", "api-token") is False


def test_token_lifetime_tracking():
    engine = make_engine()
    assert engine._token_seconds_left() == 0.0
    engine._yc_token = "tok"
    engine._yc_token_at = time.monotonic()
    assert engine._token_seconds_left() > engine.CAPTCHA_TTL_SECONDS - 2
    engine._yc_token_at = time.monotonic() - engine.CAPTCHA_TTL_SECONDS - 1
    assert engine._token_seconds_left() < 0
    engine._drop_token()
    assert engine._yc_token == ""
    assert engine._token_seconds_left() == 0.0


def test_submitted_api_token_is_retired_and_cleared_from_the_page():
    engine = make_engine()
    engine._yc_token = "spent"
    engine._yc_token_at = time.monotonic()
    engine._yc_token_submitted = True
    page = RecordingPage(results={"__pgCaptchaToken": 1})

    assert run(engine._retire_submitted_yescaptcha_token(page)) is True
    assert engine._yc_token == ""
    assert engine._yc_token_submitted is False
    assert page.calls[-1][1] == ("",)


def test_token_cleanup_after_driver_disconnect_does_not_log_injection_failure():
    engine = make_engine()
    engine._yc_token = "spent"
    engine._yc_token_at = time.monotonic()
    engine._yc_token_submitted = True
    logs = []
    engine.log = lambda message, kind="info": logs.append((message, kind))

    class ClosedPage:
        async def evaluate(self, *_args):
            raise RuntimeError("Connection closed while reading from the driver")

    assert run(engine._retire_submitted_yescaptcha_token(ClosedPage())) is True
    assert engine._yc_token_submitted is False
    assert not any("캡차 토큰 주입 실패" in message for message, _ in logs)


# -- widget nudging ------------------------------------------------------


def test_nudge_is_rate_limited_and_capped():
    engine = make_engine()
    page, frame = anchor_page(results={"bframe": False})

    assert run(engine._nudge_recaptcha_widget(page)) is True
    # Immediately after, the cooldown blocks it.
    assert run(engine._nudge_recaptcha_widget(page)) is False
    assert frame.clicks == 1

    # Past the cooldown the second (and last) click is allowed.
    engine._anchor_last_click -= engine.ANCHOR_CLICK_COOLDOWN + 1
    assert run(engine._nudge_recaptcha_widget(page)) is True
    assert frame.clicks == engine.ANCHOR_CLICK_MAX

    engine._anchor_last_click -= engine.ANCHOR_CLICK_COOLDOWN + 1
    assert run(engine._nudge_recaptcha_widget(page)) is False
    assert frame.clicks == engine.ANCHOR_CLICK_MAX


def test_nudge_never_interrupts_an_open_challenge():
    engine = make_engine()
    page, frame = anchor_page(results={"bframe": True})
    assert run(engine._nudge_recaptcha_widget(page)) is False
    assert frame.clicks == 0


def test_nudge_reports_a_missing_widget_instead_of_failing_silently():
    engine = make_engine()
    logged = []
    engine.log = lambda message, kind="info": logged.append(message)
    page = RecordingPage(results={"bframe": False}, frames=[])
    assert run(engine._nudge_recaptcha_widget(page)) is False
    assert any("anchor" in message for message in logged)


# -- token scheduling ----------------------------------------------------


def scheduling_engine():
    engine = make_engine()
    engine._yc_enabled = True
    engine._yc_client_key = "key"
    engine.solve_calls = 0
    engine.solve_test_flags = []

    async def fake_solve(_page, test_only=False):
        engine.solve_calls += 1
        engine.solve_test_flags.append(test_only)

    engine._solve_with_yescaptcha = fake_solve
    return engine


def drive(engine, remaining):
    async def go():
        engine._ensure_yescaptcha_token(RecordingPage(), remaining)
        task = engine._yc_task
        if task is not None:
            await task

    asyncio.run(go())


def test_no_token_is_bought_long_before_the_window_opens():
    """A v2 token dies in ~2 minutes; buying it early guarantees expiry."""
    engine = scheduling_engine()
    drive(engine, engine.CAPTCHA_SOLVE_LEAD + 600)
    assert engine.solve_calls == 0


def test_test_mode_buys_one_immediate_test_token_outside_the_lead_window():
    engine = scheduling_engine()
    engine._yc_test_mode = True

    drive(engine, engine.CAPTCHA_SOLVE_LEAD + 600)
    drive(engine, engine.CAPTCHA_SOLVE_LEAD + 500)

    assert engine.solve_calls == 1
    assert engine.solve_test_flags == [True]
    assert engine._yc_test_attempted is True


def test_test_mode_inside_the_lead_window_uses_a_real_booking_token():
    engine = scheduling_engine()
    engine._yc_test_mode = True

    drive(engine, engine.CAPTCHA_SOLVE_LEAD - 1)

    assert engine.solve_calls == 1
    assert engine.solve_test_flags == [False]


def test_immediate_test_token_stays_active_like_a_booking_token():
    engine = make_engine()
    logs = []
    engine.log = lambda message, kind="info": logs.append((message, kind))
    engine.open_at = 1.0
    engine.POLL_IDLE_SECONDS = 0.0
    engine.clock = type(
        "Clock",
        (),
        {"seconds_until": lambda _self, _target: 3600.0},
    )()
    page = RecordingPage()

    async def always_false(*_args, **_kwargs):
        return False

    async def inject_ok(*_args, **_kwargs):
        return True

    async def sitekey(_page):
        return "site-key"

    def supply_test_token(_page, _remaining):
        if not engine._yc_token:
            engine._yc_token = "test-token"
            engine._yc_token_at = time.monotonic()
            engine._yc_token_test_only = True

    def stop_after_first_wait(_message):
        engine.stop_event.set()

    engine._is_blocked = always_false
    async def empty_token(_page):
        return ""

    engine._captcha_token_value = empty_token
    engine._inject_yescaptcha_token = inject_ok
    engine._read_sitekey = sitekey
    engine._ensure_yescaptcha_token = supply_test_token
    engine.silent_tick = stop_after_first_wait

    run(engine._watch_and_submit(
        page,
        {"message": ""},
        {
            "devMode": False,
            "yescaptcha_enabled": True,
            "yescaptcha_test_mode": True,
            "yescaptcha_client_key": "key",
        },
        "2026-08-08",
        "10:40",
        "14",
        "1",
        "머니머니패키지",
        "1888",
    ))

    assert engine._yc_token == "test-token"
    assert engine._yc_token_test_only is True
    assert any("테스트 확인" in message for message, _kind in logs)


def test_token_is_bought_once_inside_the_lead_window():
    engine = scheduling_engine()
    drive(engine, engine.CAPTCHA_SOLVE_LEAD - 1)
    assert engine.solve_calls == 1


def test_captcha_lead_adapts_to_recent_slow_solves(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    engine = scheduling_engine()
    engine._yc_profile_key = engine._captcha_profile_id("key")
    for seconds in (50.0, 55.0, 60.0):
        engine._remember_captcha_solve_time(seconds)

    assert engine._captcha_lead_seconds() == 85.0
    drive(engine, 86.0)
    assert engine.solve_calls == 0
    drive(engine, 84.0)
    assert engine.solve_calls == 1


def test_unknown_open_time_buys_immediately():
    engine = scheduling_engine()
    drive(engine, None)
    assert engine.solve_calls == 1


def test_a_fresh_token_is_not_replaced():
    engine = scheduling_engine()
    engine._yc_token = "tok"
    engine._yc_token_at = time.monotonic()
    drive(engine, 0.0)
    assert engine.solve_calls == 0


def test_an_expiring_token_is_replaced():
    engine = scheduling_engine()
    engine._yc_token = "tok"
    engine._yc_token_at = (
        time.monotonic()
        - engine.CAPTCHA_TTL_SECONDS
        + engine.CAPTCHA_REFRESH_MARGIN
        - 1
    )
    drive(engine, 0.0)
    assert engine.solve_calls == 1


def test_repeated_failures_stop_burning_api_calls():
    engine = scheduling_engine()
    engine._yc_failures = engine.YESCAPTCHA_MAX_FAILURES
    drive(engine, 0.0)
    assert engine.solve_calls == 0


def test_nothing_is_bought_without_a_client_key():
    engine = scheduling_engine()
    engine._yc_client_key = ""
    drive(engine, 0.0)
    assert engine.solve_calls == 0


def test_nothing_is_bought_when_toggle_is_off_even_with_a_key():
    engine = scheduling_engine()
    engine._yc_enabled = False
    drive(engine, 0.0)
    assert engine.solve_calls == 0


def test_retries_are_spaced_out():
    """A network hiccup must not consume the whole failure budget at once."""
    engine = scheduling_engine()
    drive(engine, 0.0)
    drive(engine, 0.0)
    assert engine.solve_calls == 1
    engine._yc_last_attempt -= engine.YESCAPTCHA_RETRY_COOLDOWN + 1
    drive(engine, 0.0)
    assert engine.solve_calls == 2
