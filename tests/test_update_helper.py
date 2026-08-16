from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pengucro import update_helper


class FakeProcess:
    def __init__(self, pid: int = 777, return_code: int | None = None) -> None:
        self.pid = pid
        self.return_code = return_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = 0

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9

    def wait(self, timeout: float | None = None) -> int:
        return int(self.return_code or 0)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.value += duration


def _make_plan(tmp_path: Path, **overrides):
    data = tmp_path / "data"
    plan_dir = data / "updates" / "plans"
    plan_dir.mkdir(parents=True)
    target = tmp_path / "Pengucro.exe"
    staged = tmp_path / ".Pengucro.exe.update-700-1234-01234567-abcd.ready.exe"
    target.write_bytes(b"old executable")
    staged.write_bytes(b"new executable")
    payload = {
        "target_path": str(target.resolve()),
        "staged_path": str(staged.resolve()),
        "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
        "size": staged.stat().st_size,
        "parent_pid": 123,
        "other_pids": [],
        "version": "7.0",
        "release_sequence": 700,
        "health_nonce": "0123456789abcdef0123456789abcdef",
        "process_wait_seconds": 5,
        "health_wait_seconds": 5,
    }
    payload.update(overrides)
    plan_path = plan_dir / "release-700.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    return data, plan_path, target, staged, payload


def _filesystem_replace(target: Path, replacement: Path, backup: Path) -> None:
    os.replace(target, backup)
    os.replace(replacement, target)


def _filesystem_restore(target: Path, backup: Path, failed: Path) -> None:
    os.replace(target, failed)
    os.replace(backup, target)


def test_successfully_replaces_and_waits_for_matching_health_marker(tmp_path):
    data, plan_path, target, _staged, payload = _make_plan(tmp_path)
    launches = []

    def launch(executable, arguments, environment):
        process = FakeProcess()
        launches.append((executable, tuple(arguments), dict(environment), process))
        marker_index = arguments.index("--update-health-marker") + 1
        nonce_index = arguments.index("--update-health-nonce") + 1
        marker = Path(arguments[marker_index])
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "state": "ready",
                    "nonce": arguments[nonce_index],
                    "pid": process.pid,
                }
            ),
            encoding="utf-8",
        )
        return process

    operations = update_helper.HelperOperations(
        pid_alive=lambda _pid: False,
        target_pids=lambda _target: set(),
        replace_with_backup=_filesystem_replace,
        restore_backup=_filesystem_restore,
        launch=launch,
    )

    result = update_helper.run_update_helper(
        plan_path, operations=operations, data_directory=data
    )

    assert result == update_helper.EXIT_OK
    assert target.read_bytes() == b"new executable"
    assert not plan_path.exists()
    assert launches[0][0] == target.resolve()
    assert launches[0][2]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert launches[0][1][-2:] == ("--updated-from", payload["version"])
    status = json.loads(
        (data / "updates" / "status" / "release-700.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "success"
    assert status["release_sequence"] == 700


def test_rejects_plan_outside_local_update_plan_directory(tmp_path):
    data, plan_path, target, staged, payload = _make_plan(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")

    result = update_helper.run_update_helper(outside, data_directory=data)

    assert result == update_helper.EXIT_INVALID_PLAN
    assert target.read_bytes() == b"old executable"
    assert staged.exists()
    status = json.loads(
        (data / "updates" / "status" / "last-helper.json").read_text(encoding="utf-8")
    )
    assert status["code"] == "plan_location"


def test_rejects_staged_payload_outside_update_directory(tmp_path):
    data, plan_path, target, _staged, payload = _make_plan(tmp_path)
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    outside = outside_dir / ".Pengucro.exe.update-700-1234-01234567-abcd.ready.exe"
    outside.write_bytes(b"attacker")
    payload.update(
        staged_path=str(outside.resolve()),
        sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
        size=outside.stat().st_size,
    )
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    result = update_helper.run_update_helper(plan_path, data_directory=data)

    assert result == update_helper.EXIT_INVALID_PLAN
    assert target.read_bytes() == b"old executable"


def test_rejects_wrong_sibling_stage_filename(tmp_path):
    data, plan_path, target, staged, payload = _make_plan(tmp_path)
    wrong_name = staged.with_name("downloaded.exe")
    staged.rename(wrong_name)
    payload.update(
        staged_path=str(wrong_name.resolve()),
        sha256=hashlib.sha256(wrong_name.read_bytes()).hexdigest(),
        size=wrong_name.stat().st_size,
    )
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    result = update_helper.run_update_helper(plan_path, data_directory=data)

    assert result == update_helper.EXIT_INVALID_PLAN
    assert target.read_bytes() == b"old executable"
    status = json.loads(
        (data / "updates" / "status" / "last-helper.json").read_text(encoding="utf-8")
    )
    assert status["code"] == "staged_name"


def test_hash_mismatch_never_replaces_target(tmp_path):
    data, plan_path, target, staged, _payload = _make_plan(
        tmp_path, sha256="0" * 64
    )

    result = update_helper.run_update_helper(plan_path, data_directory=data)

    assert result == update_helper.EXIT_VERIFY_FAILED
    assert target.read_bytes() == b"old executable"
    assert staged.read_bytes() == b"new executable"
    status = json.loads(
        (data / "updates" / "status" / "release-700.json").read_text(encoding="utf-8")
    )
    assert status["code"] == "hash_mismatch"


def test_waits_for_parent_and_other_same_target_processes(tmp_path):
    data, plan_path, target, _staged, _payload = _make_plan(
        tmp_path, other_pids=[456]
    )
    clock = FakeClock()
    checks = {123: 0, 456: 0}

    def pid_alive(pid):
        checks[pid] += 1
        return checks[pid] < 3

    target_checks = 0

    def target_pids(_target):
        nonlocal target_checks
        target_checks += 1
        return {789} if target_checks < 2 else set()

    def launch(_executable, arguments, _environment):
        process = FakeProcess()
        marker = Path(arguments[arguments.index("--update-health-marker") + 1])
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"nonce": "0123456789abcdef0123456789abcdef", "pid": process.pid}),
            encoding="utf-8",
        )
        return process

    operations = update_helper.HelperOperations(
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        pid_alive=pid_alive,
        target_pids=target_pids,
        replace_with_backup=_filesystem_replace,
        restore_backup=_filesystem_restore,
        launch=launch,
    )

    result = update_helper.run_update_helper(
        plan_path, operations=operations, data_directory=data
    )

    assert result == update_helper.EXIT_OK
    assert clock.value >= update_helper.POLL_SECONDS
    assert target.read_bytes() == b"new executable"


def test_new_version_crash_rolls_back_and_restarts_old_version(tmp_path):
    data, plan_path, target, _staged, _payload = _make_plan(tmp_path)
    launches = []

    def launch(executable, arguments, environment):
        launches.append((executable, tuple(arguments), dict(environment)))
        if len(launches) == 1:
            return FakeProcess(return_code=7)
        return FakeProcess(pid=778)

    operations = update_helper.HelperOperations(
        pid_alive=lambda _pid: False,
        target_pids=lambda _target: set(),
        replace_with_backup=_filesystem_replace,
        restore_backup=_filesystem_restore,
        launch=launch,
    )

    result = update_helper.run_update_helper(
        plan_path, operations=operations, data_directory=data
    )

    assert result == update_helper.EXIT_ROLLED_BACK
    assert target.read_bytes() == b"old executable"
    assert len(launches) == 2
    assert launches[1][1] == ("--update-rollback", "7.0")
    status = json.loads(
        (data / "updates" / "status" / "release-700.json").read_text(encoding="utf-8")
    )
    assert status["state"] == "rolled_back"
    assert status["code"] == "new_version_crashed"


def test_health_timeout_stops_new_process_before_rollback(tmp_path):
    data, plan_path, target, _staged, _payload = _make_plan(tmp_path)
    clock = FakeClock()
    first = FakeProcess()
    launch_count = 0

    def launch(_executable, _arguments, _environment):
        nonlocal launch_count
        launch_count += 1
        return first if launch_count == 1 else FakeProcess(pid=778)

    operations = update_helper.HelperOperations(
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        pid_alive=lambda _pid: False,
        target_pids=lambda _target: set(),
        replace_with_backup=_filesystem_replace,
        restore_backup=_filesystem_restore,
        launch=launch,
    )

    result = update_helper.run_update_helper(
        plan_path, operations=operations, data_directory=data
    )

    assert result == update_helper.EXIT_ROLLED_BACK
    assert first.terminated is True
    assert target.read_bytes() == b"old executable"
    status = json.loads(
        (data / "updates" / "status" / "release-700.json").read_text(encoding="utf-8")
    )
    assert status["code"] == "health_timeout"


def test_health_marker_allows_pyinstaller_child_pid(tmp_path):
    marker = tmp_path / "health.json"
    marker.write_text(
        json.dumps(
            {
                "state": "ready",
                "nonce": "0123456789abcdef0123456789abcdef",
                "pid": 9002,
            }
        ),
        encoding="utf-8",
    )

    assert update_helper._health_marker_ready(
        marker,
        "0123456789abcdef0123456789abcdef",
        expected_pid=9001,
    )
