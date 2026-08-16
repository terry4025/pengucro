from __future__ import annotations

import json
import sys
from pathlib import Path

from pengucro import update_bootstrap
from pengucro.update_helper import EXIT_INVALID_PLAN


def test_dispatch_returns_none_for_normal_startup():
    called = []

    result = update_bootstrap.dispatch_update_helper(
        ["--ordinary", "value"], runner=lambda path: called.append(path) or 0
    )

    assert result is None
    assert called == []


def test_dispatches_absolute_update_plan_before_gui(tmp_path):
    plan = (tmp_path / "plan.json").resolve()
    called = []

    result = update_bootstrap.dispatch_update_helper(
        ["--apply-update", str(plan)],
        runner=lambda path: called.append(Path(path)) or 27,
    )

    assert result == 27
    assert called == [plan]


def test_helper_dispatch_fails_closed_for_extra_or_relative_arguments():
    called = []
    runner = lambda path: called.append(path) or 0

    assert (
        update_bootstrap.dispatch_update_helper(
            ["--apply-update", "relative.json"], runner=runner
        )
        == EXIT_INVALID_PLAN
    )
    assert (
        update_bootstrap.dispatch_update_helper(
            ["extra", "--apply-update", "C:\\plan.json"], runner=runner
        )
        == EXIT_INVALID_PLAN
    )
    assert called == []


def test_consumes_private_flags_and_preserves_unknown_arguments(tmp_path):
    health = (tmp_path / "updates" / "health" / "r602-test.json").resolve()
    nonce = "0123456789abcdef0123456789abcdef"

    context = update_bootstrap.consume_startup_update_args(
        [
            "--profile",
            "test-user",
            "--update-health-marker",
            str(health),
            "--unknown=value",
            f"--update-health-nonce={nonce}",
            "--updated-from",
            "6.02",
            "tail",
        ],
        data_directory=tmp_path,
    )

    assert context.remaining_args == (
        "--profile",
        "test-user",
        "--unknown=value",
        "tail",
    )
    assert context.health_marker == health
    assert context.health_nonce == nonce
    assert context.updated_from == "6.02"
    assert context.rollback_from == ""
    assert context.errors == ()


def test_double_dash_stops_private_argument_consumption(tmp_path):
    context = update_bootstrap.consume_startup_update_args(
        ["--", "--updated-from", "6.02"], data_directory=tmp_path
    )

    assert context.remaining_args == ("--", "--updated-from", "6.02")
    assert context.updated_from == ""


def test_default_consumption_removes_only_private_sys_argv(monkeypatch, tmp_path):
    health = (tmp_path / "updates" / "health" / "r602-test.json").resolve()
    nonce = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "app.exe",
            "--user-option",
            "42",
            "--update-health-marker",
            str(health),
            "--update-health-nonce",
            nonce,
        ],
    )

    context = update_bootstrap.consume_startup_update_args(data_directory=tmp_path)

    assert context.should_mark_ready
    assert sys.argv == ["app.exe", "--user-option", "42"]


def test_rejects_marker_outside_controlled_health_directory(tmp_path):
    outside = (tmp_path / "outside.json").resolve()
    context = update_bootstrap.consume_startup_update_args(
        [
            "--update-health-marker",
            str(outside),
            "--update-health-nonce",
            "0123456789abcdef0123456789abcdef",
        ],
        data_directory=tmp_path,
    )

    assert not context.should_mark_ready
    assert context.health_marker is None
    assert "invalid:health-marker" in context.errors


def test_rejects_incomplete_health_pair_and_duplicate_private_flag(tmp_path):
    context = update_bootstrap.consume_startup_update_args(
        ["--updated-from", "6.02", "--updated-from", "6.03", "--update-health-nonce", "bad"],
        data_directory=tmp_path,
    )

    assert not context.should_mark_ready
    assert context.updated_from == "6.02"
    assert "duplicate:--updated-from" in context.errors
    assert "invalid:health-nonce" in context.errors
    assert "incomplete:health-pair" in context.errors


def test_writes_exact_atomic_health_payload_without_pid(tmp_path):
    marker = (tmp_path / "updates" / "health" / "r602-test.json").resolve()
    nonce = "0123456789abcdef0123456789abcdef"
    context = update_bootstrap.consume_startup_update_args(
        [
            "--update-health-marker",
            str(marker),
            "--update-health-nonce",
            nonce,
            "--updated-from",
            "6.02",
        ],
        data_directory=tmp_path,
    )

    assert update_bootstrap.write_update_health_marker(
        context, data_directory=tmp_path
    )
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "state": "ready",
        "nonce": nonce,
    }
    assert not list(marker.parent.glob("*.tmp"))


def test_health_write_revalidates_context_path_and_nonce(tmp_path):
    outside = (tmp_path / "outside.json").resolve()
    forged = update_bootstrap.StartupUpdateContext(
        remaining_args=(),
        health_marker=outside,
        health_nonce="0123456789abcdef0123456789abcdef",
    )
    invalid_nonce = update_bootstrap.StartupUpdateContext(
        remaining_args=(),
        health_marker=(tmp_path / "updates" / "health" / "valid.json").resolve(),
        health_nonce="not valid!",
    )

    assert not update_bootstrap.write_update_health_marker(
        forged, data_directory=tmp_path
    )
    assert not update_bootstrap.write_update_health_marker(
        invalid_nonce, data_directory=tmp_path
    )
    assert not outside.exists()


def test_rollback_version_is_consumed_without_health_marker(tmp_path):
    context = update_bootstrap.consume_startup_update_args(
        ["--update-rollback", "6.02", "--keep-me"], data_directory=tmp_path
    )

    assert context.rollback_from == "6.02"
    assert context.updated_from == ""
    assert context.remaining_args == ("--keep-me",)
    assert not context.should_mark_ready
