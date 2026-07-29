import asyncio
import threading

import pytest

from engines.keyescape_engine import (
    DEVTOOLS_BLOCK_MARKER,
    DEVTOOLS_GUARD_SCRIPT,
    KeyescapeEngine,
)


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


class FakeContext:
    def __init__(self, pages=None):
        self.pages = pages or []
        self.init_scripts = []
        self.routes = []

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))


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


# ------------------------------------------------------- failure classification


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("[예약불가] 예약이 이미 완료되었습니다.", "capacity"),
        ("[에러] 잘못된 접근입니다.", "captcha_consumed"),
        ("예약가능시간이 아닙니다. 예약오픈시간 : 11:00", "not_open"),
        ("", "retry"),
    ],
)
def test_classify_failure(message, expected):
    assert KeyescapeEngine._classify_failure(message) == expected
