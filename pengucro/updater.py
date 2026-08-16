"""Secure background update checks and Windows-safe executable replacement."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from pengucro.storage import data_path, get_data_dir
from pengucro.update_manifest import (
    MAX_MANIFEST_BYTES,
    ManifestValidationError,
    UpdateConfig,
    UpdateError,
    UpdateManifest,
    parse_and_verify_manifest,
    validate_https_url,
)


MAX_REDIRECTS = 5
MAX_UPDATE_BYTES = 512 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 128 * 1024
HELPER_FLAG = "--apply-update"
DEFAULT_UPDATE_ARTIFACT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _manifest_check_url(url: str, release_sequence: int) -> str:
    """Avoid GitHub's stale ``releases/latest`` redirect cache per check."""
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "pengucro_check"
    ]
    minute_bucket = int(time.time() // 60)
    query.append(("pengucro_check", f"{release_sequence}-{minute_bucket}"))
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(query),
        parsed.fragment,
    ))


class UpdateNetworkError(UpdateError):
    """An update request failed before a trusted payload was obtained."""


class UpdateDownloadError(UpdateError):
    """The update binary could not be downloaded or verified."""


class UpdateApplyError(UpdateError):
    """The staged executable could not be installed safely."""


class UpdateCheckStatus(str, Enum):
    AVAILABLE = "available"
    UP_TO_DATE = "up_to_date"
    ERROR = "error"


@dataclass(frozen=True)
class UpdateCheckResult:
    status: UpdateCheckStatus
    manifest: UpdateManifest | None = None
    error: str = ""

    @property
    def available(self) -> bool:
        return self.status is UpdateCheckStatus.AVAILABLE and self.manifest is not None


@dataclass(frozen=True)
class StagedUpdate:
    manifest: UpdateManifest
    path: Path
    target_executable: Path


ProgressCallback = Callable[[int, int], None]
CheckCallback = Callable[[UpdateCheckResult], None]


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _safe_get(
    session: requests.Session,
    url: str,
    config: UpdateConfig,
    *,
    stream: bool,
) -> requests.Response:
    """GET a URL while validating every redirect before following it."""

    current = validate_https_url(url, config.allowed_hosts)
    for redirect_count in range(MAX_REDIRECTS + 1):
        try:
            response = session.get(
                current,
                allow_redirects=False,
                stream=stream,
                timeout=(config.connect_timeout_seconds, config.read_timeout_seconds),
                headers={"User-Agent": "Pengucro-Updater/1"},
            )
        except requests.RequestException as exc:
            raise UpdateNetworkError(f"업데이트 서버 연결 실패 ({type(exc).__name__})") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if status in {301, 302, 303, 307, 308}:
            if redirect_count >= MAX_REDIRECTS:
                _close_response(response)
                raise UpdateNetworkError("업데이트 서버의 리디렉션 횟수가 너무 많습니다.")
            location = str(getattr(response, "headers", {}).get("Location", ""))
            redirected = urljoin(current, location)
            try:
                current = validate_https_url(redirected, config.allowed_hosts)
            except ManifestValidationError as exc:
                _close_response(response)
                raise UpdateNetworkError("업데이트 서버가 허용되지 않은 주소로 이동했습니다.") from exc
            _close_response(response)
            continue
        if status != 200:
            _close_response(response)
            raise UpdateNetworkError(f"업데이트 서버 응답 오류 (HTTP {status or 'unknown'})")
        return response
    raise UpdateNetworkError("업데이트 서버 응답을 확인할 수 없습니다.")


def _read_limited(response: requests.Response, maximum: int) -> bytes:
    content_length = response.headers.get("Content-Length", "")
    if content_length:
        try:
            announced = int(content_length)
            if announced < 0 or announced > maximum:
                raise UpdateNetworkError("업데이트 정보가 허용 크기를 초과했습니다.")
        except ValueError as exc:
            raise UpdateNetworkError("업데이트 정보 크기 헤더가 올바르지 않습니다.") from exc
    result = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=16 * 1024):
            if not chunk:
                continue
            result.extend(chunk)
            if len(result) > maximum:
                raise UpdateNetworkError("업데이트 정보가 허용 크기를 초과했습니다.")
    except requests.RequestException as exc:
        raise UpdateNetworkError(f"업데이트 정보 수신 실패 ({type(exc).__name__})") from exc
    return bytes(result)


class UpdateService:
    """Performs authenticated update checks without blocking the UI thread."""

    def __init__(
        self,
        config: UpdateConfig,
        current_release_sequence: int,
        *,
        session: requests.Session | None = None,
    ) -> None:
        if isinstance(current_release_sequence, bool) or not isinstance(current_release_sequence, int):
            raise TypeError("current_release_sequence must be an integer")
        if current_release_sequence <= 0:
            raise ValueError("current_release_sequence must be positive")
        self.config = config
        self.current_release_sequence = current_release_sequence
        self.session = session or requests.Session()

    def check_now(self) -> UpdateCheckResult:
        response: requests.Response | None = None
        try:
            response = _safe_get(
                self.session,
                _manifest_check_url(
                    self.config.manifest_url, self.current_release_sequence
                ),
                self.config,
                stream=True,
            )
            payload = _read_limited(response, MAX_MANIFEST_BYTES)
            manifest = parse_and_verify_manifest(payload, self.config)
            status = (
                UpdateCheckStatus.AVAILABLE
                if manifest.is_newer_than(self.current_release_sequence)
                else UpdateCheckStatus.UP_TO_DATE
            )
            return UpdateCheckResult(status=status, manifest=manifest)
        except (UpdateError, OSError) as exc:
            return UpdateCheckResult(
                status=UpdateCheckStatus.ERROR,
                error=str(exc) or type(exc).__name__,
            )
        finally:
            if response is not None:
                _close_response(response)

    def check_in_background(self, callback: CheckCallback) -> threading.Thread:
        """Run one check on a daemon thread and invoke ``callback`` once."""

        if not callable(callback):
            raise TypeError("callback must be callable")

        def worker() -> None:
            result = self.check_now()
            try:
                callback(result)
            except Exception:
                # A UI callback failure must not turn into an unhandled thread
                # exception or retry the network request.
                return

        thread = threading.Thread(target=worker, name="PengucroUpdateCheck", daemon=True)
        thread.start()
        return thread

    def download(
        self,
        manifest: UpdateManifest,
        target_executable: str | os.PathLike[str],
        *,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> StagedUpdate:
        return download_update(
            manifest,
            target_executable,
            config=self.config,
            session=self.session,
            progress=progress,
            cancel_event=cancel_event,
        )


def try_create_update_service(
    current_release_sequence: int,
    *,
    environ: dict[str, str] | None = None,
    embedded_public_key_b64: str | None = None,
    session: requests.Session | None = None,
) -> tuple[UpdateService | None, str]:
    """Construct an updater or return a fail-closed reason for diagnostics."""

    try:
        config = UpdateConfig.load(
            environ=environ,
            embedded_public_key_b64=embedded_public_key_b64,
        )
        return UpdateService(config, current_release_sequence, session=session), ""
    except (UpdateError, TypeError, ValueError) as exc:
        return None, str(exc) or type(exc).__name__


def _content_length(response: requests.Response) -> int | None:
    value = response.headers.get("Content-Length", "")
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise UpdateDownloadError("업데이트 파일 크기 헤더가 올바르지 않습니다.") from exc
    if parsed < 0:
        raise UpdateDownloadError("업데이트 파일 크기 헤더가 올바르지 않습니다.")
    return parsed


def download_update(
    manifest: UpdateManifest,
    target_executable: str | os.PathLike[str],
    *,
    config: UpdateConfig,
    session: requests.Session | None = None,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> StagedUpdate:
    """Download and verify an update beside the executable it will replace."""

    if manifest.size > MAX_UPDATE_BYTES:
        raise UpdateDownloadError("업데이트 파일이 허용 크기를 초과했습니다.")
    validate_https_url(manifest.download_url, config.allowed_hosts)
    target = Path(target_executable).expanduser().resolve()
    if not target.parent.is_dir() or not target.is_file() or target.suffix.lower() != ".exe":
        raise UpdateDownloadError("업데이트할 실행 파일을 찾을 수 없습니다.")
    unique = f"{os.getpid()}-{uuid.uuid4().hex}"
    partial = target.parent / f".{target.name}.update-{manifest.release_sequence}-{unique}.part"
    ready = target.parent / f".{target.name}.update-{manifest.release_sequence}-{unique}.ready.exe"
    client = session or requests.Session()
    response: requests.Response | None = None
    received = 0
    digest = hashlib.sha256()
    try:
        response = _safe_get(client, manifest.download_url, config, stream=True)
        announced = _content_length(response)
        if announced is not None and announced != manifest.size:
            raise UpdateDownloadError("업데이트 파일 크기가 서명된 정보와 다릅니다.")
        with partial.open("xb") as stream:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                if cancel_event is not None and cancel_event.is_set():
                    raise UpdateDownloadError("업데이트 다운로드가 취소되었습니다.")
                if not chunk:
                    continue
                received += len(chunk)
                if received > manifest.size:
                    raise UpdateDownloadError("업데이트 파일이 서명된 크기를 초과했습니다.")
                digest.update(chunk)
                stream.write(chunk)
                if progress is not None:
                    progress(received, manifest.size)
            stream.flush()
            os.fsync(stream.fileno())
        if received != manifest.size:
            raise UpdateDownloadError("업데이트 파일 다운로드가 완료되지 않았습니다.")
        if digest.hexdigest() != manifest.sha256:
            raise UpdateDownloadError("업데이트 파일 무결성 확인에 실패했습니다.")
        os.replace(partial, ready)
        return StagedUpdate(manifest=manifest, path=ready, target_executable=target)
    except UpdateDownloadError:
        raise
    except UpdateError as exc:
        raise UpdateDownloadError(str(exc) or type(exc).__name__) from exc
    except (OSError, requests.RequestException) as exc:
        raise UpdateDownloadError(f"업데이트 파일 저장 실패 ({type(exc).__name__})") from exc
    finally:
        if response is not None:
            _close_response(response)
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def _canonical_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _path_key(path: str | os.PathLike[str]) -> str:
    return hashlib.sha256(_canonical_path(path).encode("utf-8")).hexdigest()[:24]


def cleanup_stale_update_artifacts(
    *,
    data_directory: str | os.PathLike[str] | None = None,
    target_executable: str | os.PathLike[str] | None = None,
    max_age_seconds: float = DEFAULT_UPDATE_ARTIFACT_MAX_AGE_SECONDS,
    now: float | None = None,
) -> tuple[Path, ...]:
    """Remove only recognized, old updater artifacts from controlled folders.

    Target-sibling downloads and rollback files are deleted only when their
    names match this updater's exact convention. A helper executable is kept
    whenever a live process is still using that exact file path.
    """

    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must not be negative")
    current_time = time.time() if now is None else float(now)
    root = (
        Path(data_directory).expanduser().resolve()
        if data_directory is not None
        else get_data_dir().resolve()
    ) / "updates"
    removed: list[Path] = []

    def old_enough(path: Path) -> bool:
        try:
            return current_time - path.stat().st_mtime >= max_age_seconds
        except OSError:
            return False

    controlled_patterns = {
        root / "plans": ("r*.json", ".r*.tmp"),
        root / "status": ("r*.json", ".r*.tmp", "last-helper.json"),
        root / "health": ("r*.json", ".r*.tmp"),
    }
    for directory, patterns in controlled_patterns.items():
        if not directory.is_dir():
            continue
        for pattern in patterns:
            for candidate in directory.glob(pattern):
                if candidate.is_symlink() or not candidate.is_file() or not old_enough(candidate):
                    continue
                try:
                    candidate.unlink()
                    removed.append(candidate)
                except OSError:
                    pass

    helper_directory = root / "helpers"
    if helper_directory.is_dir():
        for candidate in helper_directory.glob("PengucroUpdater-*-*.exe"):
            if candidate.is_symlink() or not candidate.is_file() or not old_enough(candidate):
                continue
            try:
                pid_text = candidate.name.split("-", 2)[1]
                creator_pid = int(pid_text)
            except (IndexError, ValueError):
                continue
            if _process_matches_executable(creator_pid, candidate):
                continue
            try:
                candidate.unlink()
                removed.append(candidate)
            except OSError:
                pass

    if target_executable is not None:
        target = Path(target_executable).expanduser().resolve()
        if target.parent.is_dir() and target.is_file():
            safe_patterns = (
                f".{target.name}.update-*-*-*.part",
                f".{target.name}.update-*-*-*.ready.exe",
                f".{target.stem}.update-*.backup.exe",
                f".{target.stem}.update-*.failed.exe",
            )
            for pattern in safe_patterns:
                for candidate in target.parent.glob(pattern):
                    if candidate.is_symlink() or not candidate.is_file() or not old_enough(candidate):
                        continue
                    if _process_matches_executable(os.getpid(), candidate):
                        continue
                    try:
                        candidate.unlink()
                        removed.append(candidate)
                    except OSError:
                        pass
    return tuple(removed)


def _process_matches_executable(pid: int, executable: str | os.PathLike[str]) -> bool:
    if pid <= 0:
        return False
    expected = _canonical_path(executable)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            capacity = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            query = kernel32.QueryFullProcessImageNameW
            query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            query.restype = wintypes.BOOL
            if not query(handle, 0, buffer, ctypes.byref(capacity)):
                return False
            return _canonical_path(buffer.value) == expected
        finally:
            kernel32.CloseHandle(handle)
    proc_link = Path("/proc") / str(pid) / "exe"
    if proc_link.exists():
        try:
            return _canonical_path(os.readlink(proc_link)) == expected
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


@dataclass
class InstanceLease:
    pid: int
    executable: Path
    record_path: Path
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.record_path.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self) -> "InstanceLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class ExecutableInstanceRegistry:
    """Tracks live app processes associated with one exact executable path."""

    def __init__(
        self,
        executable: str | os.PathLike[str] | None = None,
        registry_directory: str | os.PathLike[str] | None = None,
    ) -> None:
        chosen = executable or sys.executable
        self.executable = Path(_canonical_path(chosen))
        self.path_key = _path_key(self.executable)
        self.directory = (
            Path(registry_directory).expanduser().resolve()
            if registry_directory is not None
            else data_path("update_instances").resolve()
        )
        self.directory.mkdir(parents=True, exist_ok=True)

    def register(self, pid: int | None = None) -> InstanceLease:
        process_id = os.getpid() if pid is None else int(pid)
        if process_id <= 0:
            raise ValueError("pid must be positive")
        token = uuid.uuid4().hex
        record = self.directory / f"{self.path_key}-{process_id}-{token}.json"
        payload = {
            "schema_version": 1,
            "pid": process_id,
            "executable": _canonical_path(self.executable),
            "token": token,
            "started_at": time.time(),
        }
        handle, temporary = tempfile.mkstemp(prefix=f".{record.name}.", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, record)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        lease = InstanceLease(process_id, self.executable, record)
        atexit.register(lease.close)
        return lease

    def active_pids(self, *, ignore_pids: Iterable[int] = ()) -> tuple[int, ...]:
        ignored = {int(pid) for pid in ignore_pids}
        active: set[int] = set()
        for record in self.directory.glob(f"{self.path_key}-*.json"):
            try:
                values = json.loads(record.read_text(encoding="utf-8"))
                pid = int(values["pid"])
                recorded_executable = _canonical_path(values["executable"])
                valid = recorded_executable == _canonical_path(self.executable)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                valid = False
                pid = -1
            if valid and pid not in ignored and _process_matches_executable(pid, self.executable):
                active.add(pid)
                continue
            try:
                record.unlink(missing_ok=True)
            except OSError:
                pass
        return tuple(sorted(active))

    def wait_until_empty(
        self,
        *,
        timeout_seconds: float = 120.0,
        poll_seconds: float = 0.1,
        ignore_pids: Iterable[int] = (),
    ) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            if not self.active_pids(ignore_pids=ignore_pids):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.01, poll_seconds))


def read_latest_helper_status(
    *, data_directory: str | os.PathLike[str] | None = None
) -> dict[str, object] | None:
    """Read the newest bounded helper status for UI feedback on next launch."""

    root = (
        Path(data_directory).expanduser().resolve()
        if data_directory is not None
        else get_data_dir().resolve()
    )
    status_directory = root / "updates" / "status"
    try:
        candidates = [
            path
            for path in status_directory.glob("*.json")
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= 64 * 1024
        ]
        if not candidates:
            return None
        newest = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        value = json.loads(newest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    state = value.get("state")
    code = value.get("code")
    message = value.get("message")
    if not all(isinstance(item, str) and item for item in (state, code, message)):
        return None
    return value


@dataclass(frozen=True)
class PreparedUpdate:
    process: subprocess.Popen[bytes]
    plan_path: Path
    status_path: Path
    health_marker: Path


def prepare_and_launch_helper(
    staged: StagedUpdate,
    *,
    registry: ExecutableInstanceRegistry | None = None,
    lease: InstanceLease | None = None,
    restart_args: Sequence[str] = (),
    parent_pid: int | None = None,
) -> PreparedUpdate:
    """Write a validated helper plan and launch a detached updater copy.

    The caller should close its lease and exit only after this function returns.
    The helper independently re-enumerates all processes using the exact target
    path, so stale or incomplete registry records cannot make replacement race
    a still-running copy.
    """

    target = staged.target_executable.resolve()
    staged_path = staged.path.resolve()
    if staged_path.parent != target.parent:
        raise UpdateApplyError("업데이트 파일 위치가 올바르지 않습니다.")
    process_id = os.getpid() if parent_pid is None else int(parent_pid)
    if process_id <= 0:
        raise UpdateApplyError("현재 프로그램의 프로세스 정보를 확인할 수 없습니다.")
    active_registry = registry or ExecutableInstanceRegistry(target)
    if _canonical_path(active_registry.executable) != _canonical_path(target):
        raise UpdateApplyError("실행 인스턴스 정보가 현재 프로그램과 일치하지 않습니다.")
    ignored = {process_id}
    if lease is not None:
        if _canonical_path(lease.executable) != _canonical_path(target):
            raise UpdateApplyError("실행 인스턴스 정보가 현재 프로그램과 일치하지 않습니다.")
        ignored.add(lease.pid)
    other_pids = active_registry.active_pids(ignore_pids=ignored)

    update_root = get_data_dir().resolve() / "updates"
    plans_directory = update_root / "plans"
    status_directory = update_root / "status"
    health_directory = update_root / "health"
    helper_directory = update_root / "helpers"
    for directory in (plans_directory, status_directory, health_directory, helper_directory):
        directory.mkdir(parents=True, exist_ok=True)
    plan_id = f"r{staged.manifest.release_sequence}-{process_id}-{uuid.uuid4().hex}"
    plan_path = (plans_directory / f"{plan_id}.json").resolve()
    status_path = (status_directory / f"{plan_id}.json").resolve()
    health_marker = (health_directory / f"{plan_id}.json").resolve()
    health_nonce = uuid.uuid4().hex
    plan = {
        "target_path": str(target),
        "staged_path": str(staged_path),
        "sha256": staged.manifest.sha256,
        "size": staged.manifest.size,
        "parent_pid": process_id,
        "other_pids": list(other_pids),
        "version": staged.manifest.version,
        "release_sequence": staged.manifest.release_sequence,
        "status_path": str(status_path),
        "health_marker": str(health_marker),
        "health_nonce": health_nonce,
        "launch_args": list(restart_args),
        "process_wait_seconds": 120.0,
        "health_wait_seconds": 30.0,
    }
    descriptor, temporary = tempfile.mkstemp(prefix=f".{plan_path.name}.", suffix=".tmp", dir=plans_directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, plan_path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

    if getattr(sys, "frozen", False):
        helper_path = helper_directory / f"PengucroUpdater-{process_id}-{uuid.uuid4().hex}.exe"
        try:
            shutil.copy2(sys.executable, helper_path)
        except OSError as exc:
            raise UpdateApplyError(f"업데이트 도우미 준비 실패 ({type(exc).__name__})") from exc
        command = [str(helper_path), HELPER_FLAG, str(plan_path)]
    else:
        # Source-mode integration tests and developers dispatch this flag from
        # app.py just like the frozen executable.
        command = [sys.executable, str(Path(sys.argv[0]).resolve()), HELPER_FLAG, str(plan_path)]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    try:
        environment = os.environ.copy()
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        process = subprocess.Popen(
            command,
            cwd=str(target.parent),
            creationflags=creationflags,
            close_fds=True,
            env=environment,
        )
        return PreparedUpdate(process, plan_path, status_path, health_marker)
    except OSError as exc:
        try:
            plan_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpdateApplyError(f"업데이트 도우미 실행 실패 ({type(exc).__name__})") from exc


def launch_apply_helper(staged: StagedUpdate, **kwargs: object) -> subprocess.Popen[bytes]:
    """Compatibility wrapper returning only the helper process."""

    return prepare_and_launch_helper(staged, **kwargs).process
