"""Thread-safe bridge between the desktop UI and the updater services.

The controller owns update-operation state, but deliberately knows nothing
about booking engines.  The application injects one busy predicate covering
both active bookings and catalog refreshes.  Worker threads never call Tk
widgets directly; they enqueue work consumed by a small UI-thread pump.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from pengucro.update_manifest import UpdateError, UpdateManifest
from pengucro.updater import (
    ExecutableInstanceRegistry,
    InstanceLease,
    PreparedUpdate,
    StagedUpdate,
    UpdateCheckResult,
    UpdateCheckStatus,
    UpdateService,
    prepare_and_launch_helper,
)


LOGGER = logging.getLogger(__name__)
UI_PUMP_INTERVAL_MS = 50
DEFERRED_POLL_INTERVAL_MS = 500


class UpdateWindow(Protocol):
    def set_update_state(self, state: str, **details: object) -> None: ...

    def set_update_action_handler(self, callback: Callable[[str], None] | None) -> None: ...

    def after(self, milliseconds: int, callback: Callable[[], None]) -> object: ...


BusyPredicate = Callable[[], bool]
ExitCallback = Callable[[], None]
PrepareHelper = Callable[..., PreparedUpdate]


@dataclass(frozen=True)
class UpdateControllerSnapshot:
    state: str
    checking: bool
    downloading: bool
    restarting: bool
    deferred: bool
    version: str
    progress: float | None


class UpdateController:
    """Coordinates checking, downloading and user-approved update restart."""

    def __init__(
        self,
        window: UpdateWindow,
        service: UpdateService,
        registry: ExecutableInstanceRegistry,
        lease: InstanceLease,
        *,
        busy_predicate: BusyPredicate,
        on_exit: ExitCallback,
        target_executable: str | os.PathLike[str] | None = None,
        restart_args: Sequence[str] = (),
        prepare_helper: PrepareHelper = prepare_and_launch_helper,
        ui_pump_interval_ms: int = UI_PUMP_INTERVAL_MS,
        deferred_poll_interval_ms: int = DEFERRED_POLL_INTERVAL_MS,
    ) -> None:
        if not callable(busy_predicate):
            raise TypeError("busy_predicate must be callable")
        if not callable(on_exit):
            raise TypeError("on_exit must be callable")
        if not callable(prepare_helper):
            raise TypeError("prepare_helper must be callable")
        if ui_pump_interval_ms <= 0 or deferred_poll_interval_ms <= 0:
            raise ValueError("controller polling intervals must be positive")
        if not all(isinstance(argument, str) for argument in restart_args):
            raise TypeError("restart_args must contain only strings")

        self.window = window
        self.service = service
        self.registry = registry
        self.lease = lease
        self.busy_predicate = busy_predicate
        self.on_exit = on_exit
        self.target_executable = Path(target_executable or sys.executable).expanduser().resolve()
        self.restart_args = tuple(restart_args)
        self.prepare_helper = prepare_helper
        self.ui_pump_interval_ms = int(ui_pump_interval_ms)
        self.deferred_poll_interval_ms = int(deferred_poll_interval_ms)

        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._download_cancel = threading.Event()
        self._ui_queue: queue.SimpleQueue[tuple[Callable[..., None], tuple[object, ...]]] = (
            queue.SimpleQueue()
        )
        self._pump_after_id: object | None = None
        self._deferred_after_id: object | None = None
        self._started = False
        self._checking = False
        self._downloading = False
        self._restarting = False
        self._deferred = False
        self._state = "hidden"
        self._progress: float | None = None
        self._manifest: UpdateManifest | None = None
        self._staged: StagedUpdate | None = None
        self._last_failure_action = "check"
        self._check_thread: threading.Thread | None = None
        self._download_thread: threading.Thread | None = None
        self._restart_thread: threading.Thread | None = None

    @property
    def snapshot(self) -> UpdateControllerSnapshot:
        with self._lock:
            manifest = self._manifest
            return UpdateControllerSnapshot(
                state=self._state,
                checking=self._checking,
                downloading=self._downloading,
                restarting=self._restarting,
                deferred=self._deferred,
                version=manifest.version if manifest is not None else "",
                progress=self._progress,
            )

    def start(self) -> bool:
        """Attach to the window and start exactly one background check."""

        with self._lock:
            if self._shutdown.is_set() or self._started:
                return False
            self._started = True
        self.window.set_update_action_handler(self.handle_action)
        self._schedule_ui_pump()
        return self.request_check()

    def handle_action(self, action: str) -> bool:
        """Handle the small intent vocabulary emitted by ``UpdateDialog``."""

        normalized = str(action or "").strip().lower()
        if normalized == "download":
            return self.request_download()
        if normalized == "restart":
            return self.request_restart()
        if normalized == "retry":
            with self._lock:
                failure_action = self._last_failure_action
            if failure_action == "download":
                return self.request_download()
            if failure_action == "restart":
                return self.request_restart()
            return self.request_check()
        return False

    def request_check(self) -> bool:
        with self._lock:
            if (
                self._shutdown.is_set()
                or self._checking
                or self._downloading
                or self._restarting
            ):
                return False
            self._checking = True
            self._last_failure_action = "check"
        self._publish_state("checking")
        try:
            thread = self.service.check_in_background(self._receive_check_result)
        except Exception as exc:
            with self._lock:
                self._checking = False
            self._post_ui(self._show_check_error, self._safe_error(exc))
            return False
        with self._lock:
            if self._checking:
                self._check_thread = thread
        return True

    def _receive_check_result(self, result: UpdateCheckResult) -> None:
        with self._lock:
            self._checking = False
            if self._shutdown.is_set():
                return
            self._check_thread = None
            if result.available and result.manifest is not None:
                self._manifest = result.manifest
                # A newer signed manifest supersedes a previously staged file.
                if (
                    self._staged is not None
                    and self._staged.manifest.release_sequence
                    != result.manifest.release_sequence
                ):
                    self._staged = None
            elif result.status is UpdateCheckStatus.UP_TO_DATE:
                self._manifest = None
                self._staged = None
        self._post_ui(self._apply_check_result, result)

    def _apply_check_result(self, result: UpdateCheckResult) -> None:
        if result.available and result.manifest is not None:
            self._publish_state("available")
        elif result.status is UpdateCheckStatus.UP_TO_DATE:
            self._publish_state("current")
        else:
            self._show_check_error(result.error or "업데이트 정보를 확인하지 못했습니다.")

    def request_download(self) -> bool:
        with self._lock:
            if (
                self._shutdown.is_set()
                or self._checking
                or self._downloading
                or self._restarting
                or self._manifest is None
            ):
                return False
            manifest = self._manifest
            self._downloading = True
            self._deferred = False
            self._progress = 0.0
            self._last_failure_action = "download"
            self._download_cancel.clear()
        self._cancel_deferred_poll()
        self._publish_state("downloading", progress=0.0)
        thread = threading.Thread(
            target=self._download_worker,
            args=(manifest,),
            name="PengucroUpdateDownload",
            daemon=True,
        )
        with self._lock:
            self._download_thread = thread
        thread.start()
        return True

    def _download_worker(self, manifest: UpdateManifest) -> None:
        try:
            staged = self.service.download(
                manifest,
                self.target_executable,
                progress=self._receive_download_progress,
                cancel_event=self._download_cancel,
            )
        except Exception as exc:
            with self._lock:
                self._downloading = False
                self._download_thread = None
                stopping = self._shutdown.is_set()
            if not stopping:
                self._post_ui(self._show_error, "download", self._safe_error(exc))
            return
        with self._lock:
            self._downloading = False
            self._download_thread = None
            if self._shutdown.is_set():
                return
            # Ignore a stale completion if a future controller implementation
            # permits checks to supersede downloads.
            if self._manifest is None or (
                self._manifest.release_sequence != staged.manifest.release_sequence
            ):
                return
            self._staged = staged
            self._progress = 100.0
        self._post_ui(self._publish_state, "ready")

    def _receive_download_progress(self, received: int, total: int) -> None:
        if total <= 0:
            percent = 0.0
        else:
            percent = max(0.0, min(100.0, received * 100.0 / total))
        with self._lock:
            if self._shutdown.is_set() or not self._downloading:
                return
            self._progress = percent
        self._post_ui(self._publish_state, "downloading", percent)

    def request_restart(self) -> bool:
        with self._lock:
            if (
                self._shutdown.is_set()
                or self._checking
                or self._downloading
                or self._restarting
                or self._staged is None
            ):
                return False
        deferred_message = self._restart_block_message()
        if deferred_message:
            with self._lock:
                if self._shutdown.is_set():
                    return False
                self._deferred = True
            self._publish_state("deferred", message=deferred_message)
            self._schedule_deferred_poll()
            return True

        with self._lock:
            if self._shutdown.is_set() or self._restarting or self._staged is None:
                return False
            staged = self._staged
            self._restarting = True
            self._deferred = False
            self._last_failure_action = "restart"
        self._cancel_deferred_poll()
        self._publish_state("downloading", progress=100.0, message="재시작을 준비하고 있습니다.")
        thread = threading.Thread(
            target=self._restart_worker,
            args=(staged,),
            name="PengucroUpdateRestart",
            daemon=True,
        )
        with self._lock:
            self._restart_thread = thread
        thread.start()
        return True

    def _restart_worker(self, staged: StagedUpdate) -> None:
        try:
            self.prepare_helper(
                staged,
                registry=self.registry,
                lease=self.lease,
                restart_args=self.restart_args,
            )
        except Exception as exc:
            with self._lock:
                self._restarting = False
                self._restart_thread = None
                stopping = self._shutdown.is_set()
            if not stopping:
                self._post_ui(self._show_error, "restart", self._safe_error(exc))
            return
        with self._lock:
            self._restarting = False
            self._restart_thread = None
            stopping = self._shutdown.is_set()
        if not stopping:
            self._post_ui(self._finish_restart)

    def _finish_restart(self) -> None:
        if self._shutdown.is_set():
            return
        try:
            self.on_exit()
        except Exception as exc:
            self._show_error("restart", self._safe_error(exc))

    def refresh_deferred_state(self) -> bool:
        """Re-evaluate a deferred restart without ever restarting automatically."""

        with self._lock:
            if self._shutdown.is_set() or not self._deferred:
                return False
        message = self._restart_block_message()
        if message:
            self._publish_state("deferred", message=message)
            self._schedule_deferred_poll()
            return False
        with self._lock:
            if self._shutdown.is_set():
                return False
            self._deferred = False
        self._cancel_deferred_poll()
        self._publish_state("ready")
        return True

    def _restart_block_message(self) -> str:
        busy = True
        registry_failed = False
        try:
            busy = bool(self.busy_predicate())
        except Exception:
            LOGGER.exception("Update busy predicate failed")
        try:
            ignore = {os.getpid(), self.lease.pid}
            other_pids = self.registry.active_pids(ignore_pids=ignore)
        except Exception:
            LOGGER.exception("Update instance registry check failed")
            other_pids = ()
            registry_failed = True
        if registry_failed:
            return "다른 실행 창 상태를 확인한 뒤 다시 시도합니다."
        if busy and other_pids:
            return "진행 중인 작업을 마치고 같은 프로그램의 다른 창을 닫으면 재시작할 수 있습니다."
        if busy:
            return "진행 중인 예약 또는 목록 갱신이 끝나면 재시작할 수 있습니다."
        if other_pids:
            return f"같은 프로그램의 다른 실행 창 {len(other_pids)}개를 닫으면 재시작할 수 있습니다."
        return ""

    def _schedule_deferred_poll(self) -> None:
        with self._lock:
            if (
                self._shutdown.is_set()
                or not self._deferred
                or self._deferred_after_id is not None
            ):
                return
        try:
            after_id = self.window.after(
                self.deferred_poll_interval_ms,
                self._deferred_poll_tick,
            )
        except Exception:
            return
        with self._lock:
            if self._shutdown.is_set() or not self._deferred:
                self._cancel_after_id(after_id)
            else:
                self._deferred_after_id = after_id

    def _deferred_poll_tick(self) -> None:
        with self._lock:
            self._deferred_after_id = None
        self.refresh_deferred_state()

    def _cancel_deferred_poll(self) -> None:
        with self._lock:
            after_id = self._deferred_after_id
            self._deferred_after_id = None
        self._cancel_after_id(after_id)

    def _publish_state(
        self,
        state: str,
        progress: float | None = None,
        *,
        message: str = "",
    ) -> None:
        if self._shutdown.is_set():
            return
        with self._lock:
            manifest = self._manifest
            if progress is not None:
                self._progress = progress
            current_progress = self._progress
            self._state = state
        details: dict[str, object] = {
            "version": manifest.version if manifest is not None else "",
            "notes": manifest.notes if manifest is not None else (),
            "size_bytes": manifest.size if manifest is not None else None,
            "progress": current_progress,
            "message": message,
        }
        try:
            self.window.set_update_state(state, **details)
        except Exception:
            LOGGER.exception("Update UI state rendering failed")

    def _show_error(self, action: str, message: str) -> None:
        if self._shutdown.is_set():
            return
        with self._lock:
            self._last_failure_action = action
        self._publish_state("error", message=message)

    def _show_check_error(self, message: str) -> None:
        """Keep routine background check failures out of the user's way."""

        if self._shutdown.is_set():
            return
        with self._lock:
            self._last_failure_action = "check"
        LOGGER.warning("Background update check failed: %s", message)
        self._publish_state("background_error", message=message)

    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        if isinstance(exc, UpdateError) and str(exc).strip():
            return str(exc).strip()
        text = str(exc).strip()
        return text or type(exc).__name__

    def _post_ui(self, callback: Callable[..., None], *arguments: object) -> None:
        if self._shutdown.is_set():
            return
        self._ui_queue.put((callback, arguments))

    def _schedule_ui_pump(self) -> None:
        with self._lock:
            if self._shutdown.is_set() or self._pump_after_id is not None:
                return
        try:
            after_id = self.window.after(self.ui_pump_interval_ms, self._drain_ui_queue)
        except Exception:
            return
        with self._lock:
            if self._shutdown.is_set():
                self._cancel_after_id(after_id)
            else:
                self._pump_after_id = after_id

    def _drain_ui_queue(self) -> None:
        with self._lock:
            self._pump_after_id = None
        if self._shutdown.is_set():
            return
        while True:
            try:
                callback, arguments = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*arguments)
            except Exception:
                LOGGER.exception("Update controller UI callback failed")
        self._schedule_ui_pump()

    def _cancel_after_id(self, after_id: object | None) -> None:
        if after_id is None:
            return
        cancel = getattr(self.window, "after_cancel", None)
        if callable(cancel):
            try:
                cancel(after_id)
            except Exception:
                pass

    def shutdown(self, *, join_timeout: float = 0.25) -> None:
        """Stop callbacks and cooperatively cancel a download.

        Network reads use bounded timeouts, so daemon workers may outlive this
        short UI shutdown window without keeping the process alive.
        """

        if self._shutdown.is_set():
            return
        self._shutdown.set()
        self._download_cancel.set()
        with self._lock:
            pump_after_id = self._pump_after_id
            deferred_after_id = self._deferred_after_id
            self._pump_after_id = None
            self._deferred_after_id = None
            threads = (
                self._check_thread,
                self._download_thread,
                self._restart_thread,
            )
        self._cancel_after_id(pump_after_id)
        self._cancel_after_id(deferred_after_id)
        try:
            self.window.set_update_action_handler(None)
        except Exception:
            pass
        if join_timeout <= 0:
            return
        current = threading.current_thread()
        for thread in threads:
            if thread is None or thread is current or not thread.is_alive():
                continue
            thread.join(timeout=join_timeout)
