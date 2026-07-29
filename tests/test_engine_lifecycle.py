import threading
import time

from engines.keyescape_engine import KeyescapeEngine
from engines.naver_engine import NaverEngine
from ui.main_window import MainWindow


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_keyescape_running_state_tracks_real_browser_worker(monkeypatch):
    engine = KeyescapeEngine(lambda *_args: None)
    release = threading.Event()
    monkeypatch.setattr(engine, "_run_browser_booking", lambda _data: release.wait(0.5))

    engine.start_reservation({}, 1)
    assert engine.is_running
    release.set()
    assert wait_until(lambda: not engine.is_running)


def test_naver_new_run_resets_success_latch(monkeypatch):
    """The Naver engine now runs on BaseEngine's async loop.

    It used to own a bespoke thread plus a monitor that did an unbounded
    ``join()``: a wedged Playwright thread left ``is_running`` True forever and
    the GUI's start button disabled.
    """
    engine = NaverEngine(lambda *_args: None)
    engine._success_fired = True

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(engine, "run_async_tasks", noop)

    engine.start_reservation({}, 1)
    assert wait_until(lambda: not engine.is_running)
    assert not engine._success_fired


def test_naver_clamps_worker_count_to_one(monkeypatch):
    """One account can hold one booking, so extra workers only duplicate work."""
    from engines.base_engine import BaseEngine

    messages = []
    engine = NaverEngine(lambda message, level="info": messages.append(message))

    started = {}

    def capture(self, data, num_threads, is_async=False):
        started["num_threads"] = num_threads
        started["is_async"] = is_async

    monkeypatch.setattr(BaseEngine, "start_reservation", capture)
    engine.start_reservation({}, 5)

    assert started == {"num_threads": 1, "is_async": True}
    assert any("1로 조정" in message for message in messages)


class FinishedEngine:
    is_running = False


def lifecycle_window():
    window = object.__new__(MainWindow)
    window.active_engine = FinishedEngine()
    window._engine_completion_handled = False
    window.reset_count = 0
    window._reset_cta_state = lambda: setattr(window, "reset_count", window.reset_count + 1)
    return window


def test_finished_engine_resets_gui_exactly_once():
    window = lifecycle_window()

    for _ in range(20):
        window._check_engine_finished()

    assert window.reset_count == 1
    assert window.active_engine is None


def test_second_run_can_complete_without_rearming_previous_engine():
    window = lifecycle_window()
    window._check_engine_finished()

    window.active_engine = FinishedEngine()
    window._engine_completion_handled = False
    window._check_engine_finished()
    window._check_engine_finished()

    assert window.reset_count == 2
    assert window.active_engine is None


def test_success_handled_engine_is_only_cleared_after_worker_finishes():
    window = lifecycle_window()
    window._engine_completion_handled = True

    window._check_engine_finished()

    assert window.reset_count == 0
    assert window.active_engine is None
