import queue

import ui.main_window as main_window
from engines.keyescape_timetable_collector import KeyescapeCacheResult
from ui.main_window import MainWindow


class _Recorder:
    def __init__(self):
        self.calls = []

    def set_keyescape_cache_state(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def append_log(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_startup_auto_collection_runs_once_in_background(monkeypatch):
    leases = []

    class Lease:
        def __init__(self):
            self.acquired = False
            leases.append(self)

        def acquire(self):
            self.acquired = True
            return True

    class Collector:
        def __init__(self, base_url):
            assert base_url == "https://www.keyescape.com"

        def collect(self, progress, cancel_event=None):
            assert cancel_event is not None
            result = KeyescapeCacheResult(
                12, 36, 168, 147, 21, 0,
                {"A": 33, "B": 33, "C": 15, "D": 33},
            )
            progress(type("Progress", (), {"phase": "catalog"})())
            return result

    monkeypatch.setattr(main_window, "KeyescapeAutoCollectionLease", Lease)
    monkeypatch.setattr(main_window, "KeyescapeTimetableCollector", Collector)

    window = object.__new__(MainWindow)
    window.current_status = "idle"
    window.active_engine = None
    window._catalog_refresh_running = False
    window._keyescape_cache_running = False
    window.form = _Recorder()
    window.log_panel = _Recorder()
    window.engine_event_queue = queue.Queue()

    window._start_keyescape_auto_cache_refresh()

    progress = window.engine_event_queue.get(timeout=2.0)
    completed = window.engine_event_queue.get(timeout=2.0)
    assert progress[0] == "keyescape_cache_progress"
    assert completed[0] == "keyescape_cache_done"
    assert completed[1].saved_count == 147
    assert completed[2:] == ("", True)
    assert leases[0].acquired is True
    assert window._keyescape_cache_automatic is True
