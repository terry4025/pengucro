"""Crash-safe hand-off used to replace a running frozen executable.

The GUI downloads and verifies an update before it starts this helper from a
*different* executable path.  The helper then waits until every process using
the installed executable has exited, verifies the staged payload again, swaps
the files with a Windows ``ReplaceFileW`` backup, and starts the new build.

An update is accepted only from a small JSON plan below
``%LOCALAPPDATA%\\Pengucro\\updates\\plans``.  Keeping the privileged file
operation in this deliberately narrow module makes it possible to test the
failure and rollback paths without starting the GUI.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pengucro.storage import get_data_dir


UPDATE_DIRECTORY_NAME = "updates"
PLAN_DIRECTORY_NAME = "plans"
STATUS_DIRECTORY_NAME = "status"
HEALTH_DIRECTORY_NAME = "health"
HELPER_DIRECTORY_NAME = "helpers"
MAX_PLAN_BYTES = 64 * 1024
DEFAULT_PROCESS_WAIT_SECONDS = 120.0
DEFAULT_HEALTH_WAIT_SECONDS = 30.0
POLL_SECONDS = 0.2

EXIT_OK = 0
EXIT_INVALID_PLAN = 10
EXIT_INSTANCES_RUNNING = 20
EXIT_VERIFY_FAILED = 30
EXIT_REPLACE_FAILED = 40
EXIT_ROLLED_BACK = 50
EXIT_ROLLBACK_FAILED = 51

_PLAN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_LAUNCH_ARGUMENTS = {
    "--apply-update",
    "--update-health-marker",
    "--update-health-nonce",
    "--updated-from",
    "--update-rollback",
}


class UpdateHelperError(RuntimeError):
    """A controlled update failure with a stable status/exit code."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class ChildProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True)
class UpdatePlan:
    plan_path: Path
    plan_id: str
    target_path: Path
    staged_path: Path
    expected_sha256: str
    expected_size: int
    parent_pid: int
    other_pids: tuple[int, ...]
    version: str
    release_sequence: int
    status_path: Path
    health_marker: Path
    health_nonce: str
    launch_args: tuple[str, ...]
    process_wait_seconds: float
    health_wait_seconds: float


@dataclass
class HelperOperations:
    """Replaceable system calls used by the helper and its unit tests."""

    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    pid_alive: Callable[[int], bool] = lambda pid: _pid_alive(pid)
    target_pids: Callable[[Path], set[int]] = lambda path: _target_process_ids(path)
    replace_with_backup: Callable[[Path, Path, Path], None] = (
        lambda target, replacement, backup: _replace_with_backup(target, replacement, backup)
    )
    restore_backup: Callable[[Path, Path, Path], None] = (
        lambda target, backup, failed: _restore_backup(target, backup, failed)
    )
    launch: Callable[[Path, Sequence[str], Mapping[str, str]], ChildProcess] = (
        lambda executable, arguments, environment: _launch_process(
            executable, arguments, environment
        )
    )


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _normalised_path(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _read_plan_json(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise UpdateHelperError("plan_unreadable", "업데이트 계획을 읽을 수 없습니다.", EXIT_INVALID_PLAN) from exc
    if size <= 0 or size > MAX_PLAN_BYTES:
        raise UpdateHelperError("plan_size", "업데이트 계획 크기가 올바르지 않습니다.", EXIT_INVALID_PLAN)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateHelperError("plan_json", "업데이트 계획 형식이 올바르지 않습니다.", EXIT_INVALID_PLAN) from exc
    if not isinstance(value, dict):
        raise UpdateHelperError("plan_object", "업데이트 계획은 JSON 객체여야 합니다.", EXIT_INVALID_PLAN)
    return value


def _required_text(raw: Mapping[str, Any], key: str, *, maximum: int = 512) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise UpdateHelperError("plan_field", f"업데이트 계획의 {key} 값이 올바르지 않습니다.", EXIT_INVALID_PLAN)
    return value.strip()


def _bounded_number(
    raw: Mapping[str, Any],
    key: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UpdateHelperError("plan_field", f"업데이트 계획의 {key} 값이 올바르지 않습니다.", EXIT_INVALID_PLAN)
    result = float(value)
    if not minimum <= result <= maximum:
        raise UpdateHelperError("plan_field", f"업데이트 계획의 {key} 값이 허용 범위를 벗어났습니다.", EXIT_INVALID_PLAN)
    return result


def _validated_child_path(
    raw_value: Any,
    *,
    default: Path,
    required_directory: Path,
    label: str,
) -> Path:
    candidate = default if raw_value in (None, "") else Path(str(raw_value)).expanduser()
    if not candidate.is_absolute():
        raise UpdateHelperError("plan_path", f"{label} 경로는 절대 경로여야 합니다.", EXIT_INVALID_PLAN)
    if candidate.is_symlink():
        raise UpdateHelperError("plan_path", f"{label} 경로에 심볼릭 링크를 사용할 수 없습니다.", EXIT_INVALID_PLAN)
    resolved = candidate.resolve(strict=False)
    if not _is_within(resolved, required_directory):
        raise UpdateHelperError("plan_path", f"{label} 경로가 업데이트 폴더 밖을 가리킵니다.", EXIT_INVALID_PLAN)
    return resolved


def _load_plan(plan_path: str | os.PathLike[str], data_directory: Path) -> UpdatePlan:
    update_root = (data_directory / UPDATE_DIRECTORY_NAME).resolve(strict=False)
    plans_directory = (update_root / PLAN_DIRECTORY_NAME).resolve(strict=False)
    helpers_directory = (update_root / HELPER_DIRECTORY_NAME).resolve(strict=False)
    if os.name == "nt" and getattr(sys, "frozen", False):
        frozen_executable = Path(sys.executable)
        if frozen_executable.is_symlink():
            raise UpdateHelperError(
                "helper_location",
                "업데이트 도우미 경로가 올바르지 않습니다.",
                EXIT_INVALID_PLAN,
            )
        try:
            resolved_helper = frozen_executable.resolve(strict=True)
        except OSError as exc:
            raise UpdateHelperError(
                "helper_location",
                "업데이트 도우미 경로를 확인할 수 없습니다.",
                EXIT_INVALID_PLAN,
            ) from exc
        if not _is_within(resolved_helper, helpers_directory) or not re.fullmatch(
            r"PengucroUpdater-\d+-[0-9a-f]{32}\.exe",
            resolved_helper.name,
            re.IGNORECASE,
        ):
            raise UpdateHelperError(
                "helper_location",
                "허용된 폴더의 업데이트 도우미가 아닙니다.",
                EXIT_INVALID_PLAN,
            )
    supplied = Path(plan_path).expanduser()
    if not supplied.is_absolute():
        raise UpdateHelperError("plan_path", "업데이트 계획 경로는 절대 경로여야 합니다.", EXIT_INVALID_PLAN)
    if supplied.is_symlink():
        raise UpdateHelperError("plan_path", "심볼릭 링크 업데이트 계획은 사용할 수 없습니다.", EXIT_INVALID_PLAN)
    try:
        resolved_plan = supplied.resolve(strict=True)
    except OSError as exc:
        raise UpdateHelperError("plan_unreadable", "업데이트 계획을 찾을 수 없습니다.", EXIT_INVALID_PLAN) from exc
    if not _is_within(resolved_plan, plans_directory):
        raise UpdateHelperError("plan_location", "허용된 업데이트 계획 폴더가 아닙니다.", EXIT_INVALID_PLAN)

    plan_id = resolved_plan.stem
    if not _PLAN_ID_PATTERN.fullmatch(plan_id):
        raise UpdateHelperError("plan_name", "업데이트 계획 파일 이름이 올바르지 않습니다.", EXIT_INVALID_PLAN)

    raw = _read_plan_json(resolved_plan)
    target_raw = Path(_required_text(raw, "target_path", maximum=4096)).expanduser()
    staged_raw = Path(_required_text(raw, "staged_path", maximum=4096)).expanduser()
    if not target_raw.is_absolute() or not staged_raw.is_absolute():
        raise UpdateHelperError("plan_path", "대상 및 준비 파일은 절대 경로여야 합니다.", EXIT_INVALID_PLAN)
    if target_raw.is_symlink() or staged_raw.is_symlink():
        raise UpdateHelperError("plan_path", "업데이트 파일 경로에 심볼릭 링크를 사용할 수 없습니다.", EXIT_INVALID_PLAN)
    try:
        target = target_raw.resolve(strict=True)
        staged = staged_raw.resolve(strict=True)
    except OSError as exc:
        raise UpdateHelperError("plan_path", "대상 또는 준비 파일을 찾을 수 없습니다.", EXIT_INVALID_PLAN) from exc
    if not target.is_file() or target.suffix.lower() != ".exe":
        raise UpdateHelperError("target_path", "업데이트 대상은 기존 EXE 파일이어야 합니다.", EXIT_INVALID_PLAN)
    release_sequence = raw.get("release_sequence")
    if (
        isinstance(release_sequence, bool)
        or not isinstance(release_sequence, int)
        or release_sequence <= 0
    ):
        raise UpdateHelperError("release_sequence", "릴리스 순번이 올바르지 않습니다.", EXIT_INVALID_PLAN)
    expected_staged_pattern = re.compile(
        rf"^\.{re.escape(target.name)}\.update-{release_sequence}-\d+-[A-Za-z0-9-]{{8,80}}\.ready\.exe$",
        re.IGNORECASE,
    )
    if not staged.is_file():
        raise UpdateHelperError("staged_path", "준비 파일은 일반 파일이어야 합니다.", EXIT_INVALID_PLAN)
    if _normalised_path(staged.parent) != _normalised_path(target.parent):
        raise UpdateHelperError(
            "staged_path",
            "원자 교체를 위해 준비 파일은 대상 EXE와 같은 폴더에 있어야 합니다.",
            EXIT_INVALID_PLAN,
        )
    if not expected_staged_pattern.fullmatch(staged.name):
        raise UpdateHelperError(
            "staged_name",
            "준비 파일 이름이 안전한 업데이트 규칙과 일치하지 않습니다.",
            EXIT_INVALID_PLAN,
        )
    if _normalised_path(target) == _normalised_path(staged):
        raise UpdateHelperError("same_path", "대상 파일과 준비 파일이 같습니다.", EXIT_INVALID_PLAN)
    if os.name == "nt" and _normalised_path(Path(sys.executable)) == _normalised_path(target):
        raise UpdateHelperError(
            "helper_not_detached",
            "업데이트 헬퍼는 교체 대상과 다른 경로에서 실행해야 합니다.",
            EXIT_INVALID_PLAN,
        )

    digest = _required_text(raw, "sha256", maximum=64).lower()
    if not _SHA256_PATTERN.fullmatch(digest):
        raise UpdateHelperError("sha256", "SHA-256 값이 올바르지 않습니다.", EXIT_INVALID_PLAN)
    expected_size = raw.get("size")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
        raise UpdateHelperError("size", "업데이트 파일 크기가 올바르지 않습니다.", EXIT_INVALID_PLAN)
    parent_pid = raw.get("parent_pid")
    if isinstance(parent_pid, bool) or not isinstance(parent_pid, int) or parent_pid <= 0:
        raise UpdateHelperError("parent_pid", "부모 프로세스 ID가 올바르지 않습니다.", EXIT_INVALID_PLAN)

    other_raw = raw.get("other_pids", [])
    if not isinstance(other_raw, list) or any(
        isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 for pid in other_raw
    ):
        raise UpdateHelperError("other_pids", "다른 실행 프로세스 목록이 올바르지 않습니다.", EXIT_INVALID_PLAN)
    other_pids = tuple(dict.fromkeys(int(pid) for pid in other_raw if pid != parent_pid))

    version = _required_text(raw, "version", maximum=64)
    nonce = _required_text(raw, "health_nonce", maximum=128)
    if len(nonce) < 16:
        raise UpdateHelperError("health_nonce", "상태 확인 nonce가 너무 짧습니다.", EXIT_INVALID_PLAN)

    status_directory = (update_root / STATUS_DIRECTORY_NAME).resolve(strict=False)
    health_directory = (update_root / HEALTH_DIRECTORY_NAME).resolve(strict=False)
    status = _validated_child_path(
        raw.get("status_path"),
        default=status_directory / f"{plan_id}.json",
        required_directory=status_directory,
        label="상태 파일",
    )
    health = _validated_child_path(
        raw.get("health_marker"),
        default=health_directory / f"{plan_id}.json",
        required_directory=health_directory,
        label="상태 확인 파일",
    )

    launch_raw = raw.get("launch_args", [])
    if not isinstance(launch_raw, list) or any(
        not isinstance(argument, str) or len(argument) > 2048 for argument in launch_raw
    ):
        raise UpdateHelperError("launch_args", "재시작 인자 형식이 올바르지 않습니다.", EXIT_INVALID_PLAN)
    launch_args = tuple(launch_raw)
    if any(
        argument == reserved or argument.startswith(f"{reserved}=")
        for argument in launch_args
        for reserved in _FORBIDDEN_LAUNCH_ARGUMENTS
    ):
        raise UpdateHelperError("launch_args", "예약된 업데이트 인자를 직접 지정할 수 없습니다.", EXIT_INVALID_PLAN)

    return UpdatePlan(
        plan_path=resolved_plan,
        plan_id=plan_id,
        target_path=target,
        staged_path=staged,
        expected_sha256=digest,
        expected_size=expected_size,
        parent_pid=parent_pid,
        other_pids=other_pids,
        version=version,
        release_sequence=release_sequence,
        status_path=status,
        health_marker=health,
        health_nonce=nonce,
        launch_args=launch_args,
        process_wait_seconds=_bounded_number(
            raw,
            "process_wait_seconds",
            DEFAULT_PROCESS_WAIT_SECONDS,
            5.0,
            600.0,
        ),
        health_wait_seconds=_bounded_number(
            raw,
            "health_wait_seconds",
            DEFAULT_HEALTH_WAIT_SECONDS,
            5.0,
            120.0,
        ),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_staged(plan: UpdatePlan) -> None:
    try:
        actual_size = plan.staged_path.stat().st_size
        actual_digest = _sha256(plan.staged_path)
    except OSError as exc:
        raise UpdateHelperError("staged_unreadable", "준비된 업데이트 파일을 읽을 수 없습니다.", EXIT_VERIFY_FAILED) from exc
    if actual_size != plan.expected_size:
        raise UpdateHelperError("size_mismatch", "업데이트 파일 크기 검증에 실패했습니다.", EXIT_VERIFY_FAILED)
    if not hmac.compare_digest(actual_digest, plan.expected_sha256):
        raise UpdateHelperError("hash_mismatch", "업데이트 파일 무결성 검증에 실패했습니다.", EXIT_VERIFY_FAILED)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    synchronize = 0x00100000
    query_limited = 0x1000
    handle = kernel32.OpenProcess(synchronize | query_limited, False, pid)
    if not handle:
        # Access denied still means that a process with this PID exists.
        return int(ctypes.get_last_error()) == 5
    try:
        return int(kernel32.WaitForSingleObject(handle, 0)) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def _windows_process_image(pid: int) -> Path | None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        capacity = 32768
        buffer = ctypes.create_unicode_buffer(capacity)
        length = wintypes.DWORD(capacity)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def _target_process_ids(target: Path) -> set[int]:
    wanted = _normalised_path(target)
    current_pid = os.getpid()
    matches: set[int] = set()
    if os.name == "nt":
        from ctypes import wintypes

        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.EnumProcesses.argtypes = (
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        psapi.EnumProcesses.restype = wintypes.BOOL
        capacity = 2048
        while capacity <= 65536:
            entries = (wintypes.DWORD * capacity)()
            needed = wintypes.DWORD()
            if not psapi.EnumProcesses(
                entries, ctypes.sizeof(entries), ctypes.byref(needed)
            ):
                return matches
            count = needed.value // ctypes.sizeof(wintypes.DWORD)
            if count < capacity:
                break
            capacity *= 2
        else:
            count = capacity
        for pid in entries[:count]:
            if not pid or pid == current_pid:
                continue
            image = _windows_process_image(int(pid))
            if image is not None and _normalised_path(image) == wanted:
                matches.add(int(pid))
        return matches

    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == current_pid:
                continue
            try:
                image = (entry / "exe").resolve(strict=True)
            except OSError:
                continue
            if _normalised_path(image) == wanted:
                matches.add(int(entry.name))
    return matches


def _replace_with_backup(target: Path, replacement: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        backup.unlink(missing_ok=True)
    except OSError as exc:
        raise OSError(f"stale backup removal failed: {type(exc).__name__}") from exc

    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReplaceFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
        )
        kernel32.ReplaceFileW.restype = wintypes.BOOL
        result = kernel32.ReplaceFileW(
            str(target),
            str(replacement),
            str(backup),
            0,
            None,
            None,
        )
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return

    # Test/development fallback. Production Windows always uses ReplaceFileW.
    os.replace(target, backup)
    try:
        os.replace(replacement, target)
    except Exception:
        os.replace(backup, target)
        raise


def _restore_backup(target: Path, backup: Path, failed: Path) -> None:
    if not backup.is_file():
        raise FileNotFoundError("update backup is missing")
    failed.parent.mkdir(parents=True, exist_ok=True)
    failed.unlink(missing_ok=True)
    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReplaceFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
        )
        kernel32.ReplaceFileW.restype = wintypes.BOOL
        result = kernel32.ReplaceFileW(
            str(target),
            str(backup),
            str(failed),
            0,
            None,
            None,
        )
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(target, failed)
    os.replace(backup, target)


def _launch_process(
    executable: Path,
    arguments: Sequence[str],
    environment: Mapping[str, str],
) -> subprocess.Popen[bytes]:
    creationflags = 0x00000200 if os.name == "nt" else 0  # CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        [str(executable), *arguments],
        cwd=str(executable.parent),
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def _write_status(
    path: Path,
    *,
    state: str,
    code: str,
    message: str,
    plan: UpdatePlan | None = None,
    **details: Any,
) -> None:
    payload: dict[str, Any] = {
        "state": state,
        "code": code,
        "message": message,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if plan is not None:
        payload.update(
            {
                "version": plan.version,
                "release_sequence": plan.release_sequence,
                "target_name": plan.target_path.name,
                "plan_id": plan.plan_id,
            }
        )
    payload.update(details)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass


def _wait_for_target_exit(plan: UpdatePlan, operations: HelperOperations) -> None:
    deadline = operations.monotonic() + plan.process_wait_seconds
    last_blockers: set[int] | None = None
    while True:
        blockers = set(operations.target_pids(plan.target_path))
        blockers.update(pid for pid in plan.other_pids if operations.pid_alive(pid))
        if operations.pid_alive(plan.parent_pid):
            blockers.add(plan.parent_pid)
        blockers.discard(os.getpid())
        if not blockers:
            return
        if blockers != last_blockers:
            _write_status(
                plan.status_path,
                state="waiting",
                code="instances_running",
                message="실행 중인 기존 프로그램이 종료되기를 기다립니다.",
                plan=plan,
                blocking_pids=sorted(blockers),
            )
            last_blockers = blockers
        if operations.monotonic() >= deadline:
            raise UpdateHelperError(
                "instances_running",
                "다른 프로그램 창이 실행 중이어서 업데이트를 적용하지 못했습니다.",
                EXIT_INSTANCES_RUNNING,
            )
        operations.sleep(POLL_SECONDS)


def _health_marker_ready(path: Path, nonce: str, expected_pid: int) -> bool:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return False
    if text == nonce:
        return True
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(value, dict) or value.get("nonce") != nonce:
        return False
    if value.get("state", "ready") not in {"ready", "healthy", "ok"}:
        return False
    marker_pid = value.get("pid")
    # PyInstaller onefile uses a bootloader parent plus an application child;
    # Popen reports the former while the GUI sees the latter.  The fresh marker
    # path and 128-bit nonce are the process-independent launch correlation.
    # Keep PID only as optional diagnostic metadata, never as identity proof.
    return marker_pid is None or (
        isinstance(marker_pid, int) and not isinstance(marker_pid, bool) and marker_pid > 0
    )


def _stop_child(child: ChildProcess) -> None:
    try:
        if child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=5.0)
        except (subprocess.TimeoutExpired, TimeoutError):
            child.kill()
            child.wait(timeout=5.0)
    except Exception:
        # Rollback may still work if the child exited between calls.
        pass


def _launch_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # A PyInstaller child must unpack into a fresh temporary directory rather
    # than inherit the helper's frozen-process environment.
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def _apply_plan(plan: UpdatePlan, operations: HelperOperations) -> int:
    _verify_staged(plan)
    _wait_for_target_exit(plan, operations)
    # The file can sit on disk while the parent is closing. Re-verify at the
    # last possible moment so a changed payload is never installed.
    _verify_staged(plan)

    backup = plan.target_path.with_name(
        f".{plan.target_path.stem}.update-{plan.plan_id}.backup.exe"
    )
    failed_new = plan.target_path.with_name(
        f".{plan.target_path.stem}.update-{plan.plan_id}.failed.exe"
    )
    plan.health_marker.parent.mkdir(parents=True, exist_ok=True)
    plan.health_marker.unlink(missing_ok=True)
    _write_status(
        plan.status_path,
        state="applying",
        code="replace_started",
        message="업데이트 파일을 적용하고 있습니다.",
        plan=plan,
    )

    try:
        operations.replace_with_backup(plan.target_path, plan.staged_path, backup)
    except Exception as exc:
        raise UpdateHelperError(
            "replace_failed",
            f"실행 파일 교체에 실패했습니다. ({type(exc).__name__})",
            EXIT_REPLACE_FAILED,
        ) from exc

    child: ChildProcess | None = None
    failure_code = "launch_failed"
    failure_message = "업데이트된 프로그램을 시작하지 못했습니다."
    try:
        arguments = (
            *plan.launch_args,
            "--update-health-marker",
            str(plan.health_marker),
            "--update-health-nonce",
            plan.health_nonce,
            "--updated-from",
            plan.version,
        )
        child = operations.launch(plan.target_path, arguments, _launch_environment())
        _write_status(
            plan.status_path,
            state="verifying",
            code="health_wait",
            message="새 버전의 정상 실행을 확인하고 있습니다.",
            plan=plan,
            child_pid=int(child.pid),
        )
        deadline = operations.monotonic() + plan.health_wait_seconds
        while operations.monotonic() < deadline:
            if _health_marker_ready(plan.health_marker, plan.health_nonce, int(child.pid)):
                backup_left = False
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    backup_left = True
                try:
                    plan.plan_path.unlink(missing_ok=True)
                    plan.health_marker.unlink(missing_ok=True)
                except OSError:
                    pass
                _write_status(
                    plan.status_path,
                    state="success",
                    code="updated",
                    message="업데이트가 완료되었습니다.",
                    plan=plan,
                    child_pid=int(child.pid),
                    backup_left=backup_left,
                )
                return EXIT_OK
            return_code = child.poll()
            if return_code is not None:
                failure_code = "new_version_crashed"
                failure_message = f"새 버전이 준비 완료 전에 종료되었습니다. (종료 코드 {return_code})"
                break
            operations.sleep(POLL_SECONDS)
        else:
            failure_code = "health_timeout"
            failure_message = "새 버전의 정상 실행 확인 시간이 초과되었습니다."
    except Exception as exc:
        failure_code = "launch_failed"
        failure_message = f"업데이트된 프로그램을 시작하지 못했습니다. ({type(exc).__name__})"

    if child is not None:
        _stop_child(child)
    try:
        operations.restore_backup(plan.target_path, backup, failed_new)
    except Exception as exc:
        _write_status(
            plan.status_path,
            state="failed",
            code="rollback_failed",
            message=f"업데이트 실패 후 이전 버전 복원에도 실패했습니다. ({type(exc).__name__})",
            plan=plan,
            original_failure=failure_code,
        )
        return EXIT_ROLLBACK_FAILED

    rollback_launch_error = ""
    try:
        operations.launch(
            plan.target_path,
            ("--update-rollback", plan.version),
            _launch_environment(),
        )
    except Exception as exc:
        rollback_launch_error = type(exc).__name__
    _write_status(
        plan.status_path,
        state="rolled_back",
        code=failure_code,
        message=f"{failure_message} 이전 버전으로 복원했습니다.",
        plan=plan,
        rollback_launch_error=rollback_launch_error,
    )
    return EXIT_ROLLED_BACK


def run_update_helper(
    plan_path: str | os.PathLike[str],
    *,
    operations: HelperOperations | None = None,
    data_directory: str | os.PathLike[str] | None = None,
) -> int:
    """Apply one validated update plan and return a process exit code.

    ``app.py`` should call this only for its dedicated ``--apply-update`` mode.
    Normal GUI startup must write the requested health marker as soon as its
    root window is ready.
    """

    data_dir = Path(data_directory).resolve(strict=False) if data_directory else get_data_dir()
    update_root = data_dir / UPDATE_DIRECTORY_NAME
    fallback_status = update_root / STATUS_DIRECTORY_NAME / "last-helper.json"
    plan: UpdatePlan | None = None
    try:
        plan = _load_plan(plan_path, data_dir)
        return _apply_plan(plan, operations or HelperOperations())
    except UpdateHelperError as exc:
        status = plan.status_path if plan is not None else fallback_status
        _write_status(
            status,
            state="failed",
            code=exc.code,
            message=str(exc),
            plan=plan,
        )
        return exc.exit_code
    except Exception as exc:
        status = plan.status_path if plan is not None else fallback_status
        _write_status(
            status,
            state="failed",
            code="unexpected_error",
            message=f"업데이트 도우미 오류가 발생했습니다. ({type(exc).__name__})",
            plan=plan,
        )
        return EXIT_REPLACE_FAILED
