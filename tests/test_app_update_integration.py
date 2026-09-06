from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import app
from pengucro import __release_sequence__
from pengucro.update_bootstrap import StartupUpdateContext
from pengucro.updater import try_create_update_service


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_production_update_key_enables_signed_update_service():
    service, error = try_create_update_service(__release_sequence__, environ={})

    assert service is not None
    assert error == ""
    assert service.current_release_sequence == __release_sequence__


def test_importing_app_does_not_import_gui_before_helper_dispatch():
    command = (
        "import sys; import app; "
        "assert 'customtkinter' not in sys.modules; "
        "assert 'ui.main_window' not in sys.modules"
    )

    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_malformed_helper_invocation_exits_without_opening_gui():
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "app.py"), "--apply-update", "relative.json"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 10
    assert result.stdout == ""
    assert result.stderr == ""


def test_packaged_ocr_diagnostic_dispatches_without_ui_or_reservations(monkeypatch):
    from tools import verify_zeroworld_ocr
    seen = []
    monkeypatch.setattr(sys, "argv", ["app.exe", "--verify-zeroworld-ocr", "local-corpus", "--output", "report.json"])
    monkeypatch.setattr(verify_zeroworld_ocr, "main", lambda args: seen.append(args) or 0)
    monkeypatch.setattr(app, "configure_windows_app_identity", lambda: (_ for _ in ()).throw(AssertionError("No UI")))
    assert app.main() == 0
    assert seen == [["local-corpus", "--output", "report.json"]]


def test_update_busy_covers_booking_engine_and_catalog_refresh():
    assert app._update_busy(SimpleNamespace(current_status="running"))
    assert app._update_busy(
        SimpleNamespace(
            current_status="idle",
            active_engine=SimpleNamespace(is_running=True),
            _catalog_refresh_running=False,
        )
    )
    assert app._update_busy(
        SimpleNamespace(
            current_status="idle",
            active_engine=None,
            _catalog_refresh_running=True,
        )
    )
    assert not app._update_busy(
        SimpleNamespace(
            current_status="idle",
            active_engine=None,
            _catalog_refresh_running=False,
        )
    )


def test_update_startup_log_uses_display_version_without_v_prefix():
    messages = []

    class Panel:
        def append_log(self, message, level):
            messages.append((message, level))

    app._report_update_startup(
        type("Window", (), {"log_panel": Panel()})(),
        StartupUpdateContext(remaining_args=(), updated_from="6.04"),
    )

    assert messages == [("[정보] 6.04 업데이트 적용 후 정상적으로 시작했습니다.", "success")]
