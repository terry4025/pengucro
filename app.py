from __future__ import annotations

import logging
import multiprocessing
import os
import sys
from pathlib import Path

from pengucro import __version__, __release_sequence__, logging_setup
from pengucro.update_bootstrap import (
    StartupUpdateContext,
    consume_startup_update_args,
    dispatch_update_helper,
    write_update_health_marker,
)


LOGGER = logging.getLogger(__name__)


def configure_windows_app_identity() -> None:
    """Give Windows a stable identity for taskbar grouping and icon lookup."""

    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "terry4025.pengucro"
        )
    except Exception:
        pass


def _is_frozen_windows_executable() -> bool:
    """Only a packaged EXE may replace itself; never target developer Python."""

    return bool(
        os.name == "nt"
        and getattr(sys, "frozen", False)
        and Path(sys.executable).suffix.lower() == ".exe"
    )


def _update_busy(window: object) -> bool:
    engine = getattr(window, "active_engine", None)
    return bool(
        getattr(window, "current_status", "idle") in {"running", "stopping"}
        or (engine is not None and getattr(engine, "is_running", False))
        or getattr(window, "_catalog_refresh_running", False)
        or getattr(window, "_keyescape_cache_running", False)
    )


def _start_update_controller_when_idle(window: object, controller: object) -> None:
    """Keep update traffic away from reservation and catalog critical paths."""

    if _update_busy(window):
        try:
            window.after(
                5000,
                lambda: _start_update_controller_when_idle(window, controller),
            )
        except Exception:
            pass
        return
    try:
        controller.start()
    except Exception as exc:
        LOGGER.warning("Automatic update check could not start (%s)", type(exc).__name__)


def _report_update_startup(window: object, context: StartupUpdateContext) -> None:
    """Acknowledge a helper restart only after the Tk event loop is responsive."""

    if context.errors:
        LOGGER.warning(
            "Updater restart arguments were rejected (%s)",
            ",".join(context.errors),
        )

    if context.should_mark_ready:
        if write_update_health_marker(context):
            LOGGER.info("Updated application health marker written")
        else:
            # The detached helper will time out and restore the previous EXE.
            LOGGER.error("Updated application health marker could not be written")

    log_panel = getattr(window, "log_panel", None)
    if context.updated_from and log_panel is not None:
        log_panel.append_log(
            f"[정보] {context.updated_from} 업데이트 적용 후 정상적으로 시작했습니다.",
            "success",
        )
    elif context.rollback_from and log_panel is not None:
        log_panel.append_log(
            f"[경고] {context.rollback_from} 업데이트 시작에 실패해 이전 버전으로 복구했습니다.",
            "warning",
        )


def _configure_updater(window: object):
    """Attach the updater to a packaged app, leaving source runs untouched."""

    if not _is_frozen_windows_executable():
        LOGGER.debug("Automatic updater disabled for a non-frozen run")
        return None, None

    from pengucro.update_controller import UpdateController
    from pengucro.updater import (
        ExecutableInstanceRegistry,
        cleanup_stale_update_artifacts,
        try_create_update_service,
    )

    target = Path(sys.executable).resolve()
    try:
        cleanup_stale_update_artifacts(target_executable=target)
    except Exception as exc:
        LOGGER.warning("Stale update cleanup failed (%s)", type(exc).__name__)

    service, disabled_reason = try_create_update_service(__release_sequence__)
    if service is None:
        # No update UI is shown when trusted release configuration is absent.
        LOGGER.info("Automatic updater unavailable: %s", disabled_reason)
        return None, None

    registry = None
    lease = None
    try:
        registry = ExecutableInstanceRegistry(target)
        lease = registry.register()
        controller = UpdateController(
            window,
            service,
            registry,
            lease,
            busy_predicate=lambda: _update_busy(window),
            on_exit=window._on_close,
            target_executable=target,
            restart_args=tuple(sys.argv[1:]),
        )
        # Delay the tiny manifest request until initial UI/catalog work settles;
        # if a reservation starts meanwhile, wait until its critical path ends.
        window.after(
            2500,
            lambda: _start_update_controller_when_idle(window, controller),
        )
        return controller, lease
    except Exception as exc:
        if lease is not None:
            lease.close()
        LOGGER.warning("Automatic updater initialization failed (%s)", type(exc).__name__)
        return None, None


def main() -> int:
    # A copied one-file EXE runs this narrow path while the normal executable is
    # closed. Dispatch it before importing CustomTkinter or any reservation UI.
    helper_exit_code = dispatch_update_helper()
    if helper_exit_code is not None:
        return helper_exit_code

    startup_update = consume_startup_update_args()
    configure_windows_app_identity()

    # Install the rotating log handler before importing the full UI so catalog
    # and engine diagnostics emitted during start-up are captured.
    log_path = logging_setup.configure()
    LOGGER.info("Pengucro starting v%s (release=%s, log file: %s)",
                __version__, __release_sequence__, log_path)

    if "--dpsnnn-plan" in sys.argv:
        index = sys.argv.index("--dpsnnn-plan")
        if index + 1 >= len(sys.argv):
            return 2
        from ui.dpsnnn_batch import run_plan_window
        return run_plan_window(sys.argv[index + 1])

    # A fresh installation has no timetable history. Import only missing,
    # integrity-checked public Keyescape rows bundled with this exact build;
    # the signed update manifest covers the executable containing this seed.
    try:
        from engines.keyescape_schedule_cache import merge_bundled_slot_templates

        seed_result = merge_bundled_slot_templates()
        if seed_result.imported:
            LOGGER.info(
                "Bundled Keyescape timetable seed imported (%s rows)",
                seed_result.imported,
            )
        elif seed_result.available and seed_result.rejected:
            LOGGER.warning("Bundled Keyescape timetable seed was rejected")
    except Exception as exc:
        # Seed data only accelerates the guarded fast path. A packaging or
        # local-cache issue must not prevent the application from starting.
        LOGGER.warning(
            "Bundled Keyescape timetable seed could not be imported (%s)",
            type(exc).__name__,
        )

    import customtkinter as ctk

    from ui.main_window import MainWindow

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    app = MainWindow()
    update_controller = None
    instance_lease = None
    try:
        update_controller, instance_lease = _configure_updater(app)
        # A short event-loop delay proves that the real window, its controls,
        # and scheduled callbacks all initialized before the helper commits.
        app.after(300, lambda: _report_update_startup(app, startup_update))
        app.mainloop()
        return 0
    finally:
        if update_controller is not None:
            update_controller.shutdown()
        if instance_lease is not None:
            instance_lease.close()
        LOGGER.info("Pengucro exited")
        logging.shutdown()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
