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
    engine = NaverEngine(lambda *_args: None)
    engine._success_fired = True
    monkeypatch.setattr(engine, "_run_playwright_loop", lambda *_args: None)

    engine.start_reservation({}, 1)
    assert wait_until(lambda: not engine.is_running)
    assert not engine._success_fired


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
