from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from pengucro.update_controller import UpdateController
from pengucro.update_manifest import UpdateError, UpdateManifest
from pengucro.updater import (
    StagedUpdate,
    UpdateCheckResult,
    UpdateCheckStatus,
)


def _manifest(sequence: int = 602) -> UpdateManifest:
    body = b"new executable"
    return UpdateManifest(
        schema_version=1,
        release_sequence=sequence,
        version="6.02",
        download_url="https://github.com/example/update.exe",
        size=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        notes=("자동 업데이트 기능 추가", "진단 로그 개선"),
    )


class FakeWindow:
    def __init__(self):
        self.handler = None
        self.states = []
        self.render_threads = []
        self._callbacks = {}
        self._next_id = 0

    def set_update_action_handler(self, callback):
        self.handler = callback

    def set_update_state(self, state, **details):
        self.states.append((state, details))
        self.render_threads.append(threading.get_ident())

    def after(self, milliseconds, callback):
        self._next_id += 1
        callback_id = f"after-{self._next_id}"
        self._callbacks[callback_id] = (milliseconds, callback)
        return callback_id

    def after_cancel(self, callback_id):
        self._callbacks.pop(callback_id, None)

    def run_callbacks_once(self):
        pending = tuple(self._callbacks.items())
        self._callbacks.clear()
        for _callback_id, (_milliseconds, callback) in pending:
            callback()

    def run_deferred_once(self, deferred_delay=500):
        selected = [
            (callback_id, callback)
            for callback_id, (milliseconds, callback) in self._callbacks.items()
            if milliseconds == deferred_delay
        ]
        for callback_id, callback in selected:
            self._callbacks.pop(callback_id, None)
            callback()


class FakeRegistry:
    def __init__(self):
        self.pids = ()
        self.calls = []

    def active_pids(self, *, ignore_pids=()):
        ignored = set(ignore_pids)
        self.calls.append(ignored)
        return tuple(pid for pid in self.pids if pid not in ignored)


class FakeService:
    def __init__(self, manifest=None):
        self.manifest = manifest or _manifest()
        self.check_release = threading.Event()
        self.check_release.set()
        self.download_release = threading.Event()
        self.download_release.set()
        self.check_calls = 0
        self.download_calls = 0
        self.check_result = UpdateCheckResult(UpdateCheckStatus.AVAILABLE, self.manifest)
        self.cancel_seen = False

    def check_in_background(self, callback):
        self.check_calls += 1

        def worker():
            self.check_release.wait(2)
            callback(self.check_result)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread

    def download(self, manifest, target, *, progress, cancel_event):
        self.download_calls += 1
        progress(3, manifest.size)
        while not self.download_release.wait(0.01):
            if cancel_event.is_set():
                self.cancel_seen = True
                raise UpdateError("취소됨")
        if cancel_event.is_set():
            self.cancel_seen = True
            raise UpdateError("취소됨")
        progress(manifest.size, manifest.size)
        target_path = Path(target).resolve()
        staged_path = target_path.parent / (
            f".{target_path.name}.update-{manifest.release_sequence}-1-deadbeef.ready.exe"
        )
        return StagedUpdate(manifest, staged_path, target_path)


def _controller(
    tmp_path,
    *,
    service=None,
    registry=None,
    busy=None,
    prepare=None,
    exits=None,
):
    window = FakeWindow()
    target = tmp_path / "Pengucro.exe"
    target.write_bytes(b"current")
    service = service or FakeService()
    registry = registry or FakeRegistry()
    busy = busy or (lambda: False)
    prepare_calls = []

    def default_prepare(staged, **kwargs):
        prepare_calls.append((staged, kwargs))
        return SimpleNamespace(process=object())

    controller = UpdateController(
        window,
        service,
        registry,
        SimpleNamespace(pid=123, executable=target),
        busy_predicate=busy,
        on_exit=(lambda: exits.append("exit")) if exits is not None else lambda: None,
        target_executable=target,
        prepare_helper=prepare or default_prepare,
        ui_pump_interval_ms=1,
        deferred_poll_interval_ms=500,
    )
    return controller, window, service, registry, prepare_calls


def _pump_until(window, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        window.run_callbacks_once()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


def _make_ready(controller, window):
    assert controller.start()
    _pump_until(window, lambda: controller.snapshot.state == "available")
    assert controller.handle_action("download")
    _pump_until(window, lambda: controller.snapshot.state == "ready")


def test_background_check_publishes_available_on_ui_thread_and_deduplicates(tmp_path):
    service = FakeService()
    service.check_release.clear()
    controller, window, _service, _registry, _prepare = _controller(
        tmp_path, service=service
    )
    main_thread = threading.get_ident()

    assert controller.start()
    assert controller.request_check() is False
    assert controller.start() is False
    assert service.check_calls == 1

    service.check_release.set()
    _pump_until(window, lambda: controller.snapshot.state == "available")

    state, details = window.states[-1]
    assert state == "available"
    assert details["version"] == "6.02"
    assert details["notes"] == ("자동 업데이트 기능 추가", "진단 로그 개선")
    assert set(window.render_threads) == {main_thread}
    controller.shutdown()


def test_download_reports_progress_becomes_ready_and_prevents_duplicates(tmp_path):
    service = FakeService()
    service.download_release.clear()
    controller, window, _service, _registry, _prepare = _controller(
        tmp_path, service=service
    )
    assert controller.start()
    _pump_until(window, lambda: controller.snapshot.state == "available")

    assert window.handler("download") is True
    assert window.handler("download") is False
    _pump_until(window, lambda: controller.snapshot.progress not in (None, 0.0))
    assert controller.snapshot.state == "downloading"
    assert service.download_calls == 1

    service.download_release.set()
    _pump_until(window, lambda: controller.snapshot.state == "ready")
    assert controller.snapshot.progress == 100.0
    controller.shutdown()


def test_busy_restart_is_deferred_then_returns_to_ready_without_auto_restart(tmp_path):
    busy = {"value": True}
    exits = []
    controller, window, _service, _registry, prepare_calls = _controller(
        tmp_path,
        busy=lambda: busy["value"],
        exits=exits,
    )
    _make_ready(controller, window)

    assert controller.request_restart() is True
    assert controller.snapshot.state == "deferred"
    assert "예약 또는 목록 갱신" in window.states[-1][1]["message"]
    assert prepare_calls == []

    busy["value"] = False
    window.run_deferred_once()
    assert controller.snapshot.state == "ready"
    assert prepare_calls == []
    assert exits == []

    assert controller.request_restart() is True
    _pump_until(window, lambda: bool(exits))
    assert len(prepare_calls) == 1
    controller.shutdown()


def test_other_same_executable_instance_defers_until_closed(tmp_path):
    registry = FakeRegistry()
    registry.pids = (456,)
    controller, window, _service, _registry, prepare_calls = _controller(
        tmp_path, registry=registry
    )
    _make_ready(controller, window)

    assert controller.request_restart() is True
    assert controller.snapshot.state == "deferred"
    assert "다른 실행 창 1개" in window.states[-1][1]["message"]
    assert os.getpid() in registry.calls[-1]
    assert 123 in registry.calls[-1]

    registry.pids = ()
    window.run_deferred_once()
    assert controller.snapshot.state == "ready"
    assert prepare_calls == []
    controller.shutdown()


def test_prepare_failure_enters_error_and_retry_is_not_duplicated(tmp_path):
    attempts = []
    release = threading.Event()

    def failing_prepare(*_args, **_kwargs):
        attempts.append(1)
        release.wait(2)
        raise UpdateError("도우미 준비 실패")

    controller, window, _service, _registry, _prepare = _controller(
        tmp_path, prepare=failing_prepare
    )
    _make_ready(controller, window)

    assert controller.request_restart()
    assert controller.request_restart() is False
    release.set()
    _pump_until(window, lambda: controller.snapshot.state == "error")
    assert window.states[-1][1]["message"] == "도우미 준비 실패"
    assert len(attempts) == 1
    controller.shutdown()


def test_check_error_stays_hidden_as_background_error_and_can_be_rechecked(tmp_path, caplog):
    service = FakeService()
    service.check_result = UpdateCheckResult(UpdateCheckStatus.ERROR, error="연결 실패")
    controller, window, _service, _registry, _prepare = _controller(
        tmp_path, service=service
    )

    assert controller.start()
    _pump_until(window, lambda: controller.snapshot.state == "background_error")
    assert window.states[-1][1]["message"] == "연결 실패"
    assert "Background update check failed" in caplog.text
    service.check_release.clear()
    assert controller.request_check()
    assert controller.request_check() is False
    assert service.check_calls == 2
    service.check_release.set()
    controller.shutdown()


def test_shutdown_cancels_download_detaches_handler_and_suppresses_late_ui(tmp_path):
    service = FakeService()
    service.download_release.clear()
    controller, window, _service, _registry, _prepare = _controller(
        tmp_path, service=service
    )
    assert controller.start()
    _pump_until(window, lambda: controller.snapshot.state == "available")
    assert controller.request_download()
    _pump_until(window, lambda: controller.snapshot.progress not in (None, 0.0))
    state_count = len(window.states)

    controller.shutdown(join_timeout=1.0)

    assert window.handler is None
    assert service.cancel_seen
    window.run_callbacks_once()
    assert len(window.states) == state_count
    assert controller.request_check() is False
    assert controller.request_download() is False
    assert controller.request_restart() is False
