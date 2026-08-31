from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


APP_DIR_NAME = "Pengucro"
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def get_data_dir() -> Path:
    override = os.environ.get("PENGUCRO_DATA_DIR")
    if override:
        path = Path(override).expanduser().resolve()
    else:
        root = os.environ.get("LOCALAPPDATA")
        path = Path(root) / APP_DIR_NAME if root else Path.home() / f".{APP_DIR_NAME.lower()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_path(filename: str) -> Path:
    return get_data_dir() / filename


def migrate_legacy_file(filename: str) -> Path:
    destination = data_path(filename)
    with _exclusive_json_lock(destination):
        _migrate_legacy_file_unlocked(filename, destination)
    return destination


def _migrate_legacy_file_unlocked(filename: str, destination: Path) -> None:
    legacy = Path.cwd() / filename
    if not destination.exists() and legacy.exists() and legacy.resolve() != destination.resolve():
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".migrate", dir=destination.parent
        )
        os.close(handle)
        try:
            shutil.copy2(legacy, temporary_name)
            os.replace(temporary_name, destination)
        except OSError:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def load_json(filename: str, default: Any) -> Any:
    path = data_path(filename)
    with _exclusive_json_lock(path):
        _migrate_legacy_file_unlocked(filename, path)
        try:
            with path.open("r", encoding="utf-8") as stream:
                return json.load(stream)
        except (OSError, ValueError, TypeError):
            return default


def _thread_lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _exclusive_json_lock(path: Path, timeout_seconds: float = 5.0) -> Iterator[None]:
    """Serialize one JSON read-modify-write across threads and app processes."""

    thread_lock = _thread_lock_for(path)
    if not thread_lock.acquire(timeout=max(0.1, float(timeout_seconds))):
        raise TimeoutError(f"설정 파일 잠금 대기 시간 초과: {path.name}")
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = None
    locked = False
    try:
        stream = lock_path.open("a+b")
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"설정 파일 잠금 대기 시간 초과: {path.name}")
                time.sleep(0.01)
        yield
    finally:
        if stream is not None:
            if locked:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            stream.close()
        thread_lock.release()


def _write_json_unlocked(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(8):
            try:
                os.replace(temporary_name, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                # Windows virus scanners and indexers can briefly hold the old
                # destination even though app processes share the lock file.
                time.sleep(0.01 * (attempt + 1))
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def save_json(filename: str, value: Any) -> Path:
    path = data_path(filename)
    with _exclusive_json_lock(path):
        _write_json_unlocked(path, value)
    return path


def update_json(
    filename: str,
    updater: Callable[[Any], Any],
    default: Any,
) -> Path:
    """Atomically update shared JSON without losing another process's fields."""

    if not callable(updater):
        raise TypeError("updater must be callable")
    path = data_path(filename)
    with _exclusive_json_lock(path):
        _migrate_legacy_file_unlocked(filename, path)
        try:
            with path.open("r", encoding="utf-8") as stream:
                current = json.load(stream)
        except (OSError, ValueError, TypeError):
            current = default
        updated = updater(current)
        _write_json_unlocked(path, updated)
    return path


def append_history(record: dict[str, Any]) -> Path:
    path = data_path("reservation_history.jsonl")
    safe_record = {key: value for key, value in record.items() if key not in {"name", "phone", "cookies"}}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(safe_record, ensure_ascii=False) + "\n")
    return path


class SecretStore:
    """Stores secrets encrypted for the current Windows user with DPAPI.

    PyInstaller builds historically depended on ``pywin32`` for DPAPI. If that
    optional module was omitted from a release, ``set()`` raised before the form
    could save ordinary settings such as the selected people count. Use the
    native Windows crypt32 API as a fallback and keep secret-write failures from
    aborting the independent config.json save path.
    """

    def __init__(self, filename: str = "secrets.json") -> None:
        self.filename = filename

    @staticmethod
    def _windows_dpapi(value: bytes, *, protect: bool) -> bytes:
        if os.name != "nt":
            raise RuntimeError("Windows DPAPI는 Windows에서만 사용할 수 있습니다.")

        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        blob_ptr = ctypes.POINTER(DATA_BLOB)
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        crypt32.CryptProtectData.argtypes = [
            blob_ptr,
            wintypes.LPCWSTR,
            blob_ptr,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_ptr,
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            blob_ptr,
            ctypes.POINTER(ctypes.c_void_p),
            blob_ptr,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_ptr,
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        raw_buffer = (ctypes.c_ubyte * max(1, len(value)))()
        if value:
            ctypes.memmove(raw_buffer, value, len(value))
        in_blob = DATA_BLOB(
            len(value),
            ctypes.cast(raw_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        out_blob = DATA_BLOB()
        description_ptr = ctypes.c_void_p()

        if protect:
            ok = crypt32.CryptProtectData(
                ctypes.byref(in_blob),
                "Pengucro",
                None,
                None,
                None,
                0,
                ctypes.byref(out_blob),
            )
        else:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(in_blob),
                ctypes.byref(description_ptr),
                None,
                None,
                None,
                0,
                ctypes.byref(out_blob),
            )

        if not ok:
            raise RuntimeError(f"Windows DPAPI 처리 실패 (오류 {ctypes.get_last_error()})")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            if out_blob.pbData:
                kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
            if description_ptr.value:
                kernel32.LocalFree(description_ptr)

    @classmethod
    def _protect(cls, value: bytes) -> bytes:
        try:
            import win32crypt
        except ImportError:
            return cls._windows_dpapi(value, protect=True)
        try:
            protected = win32crypt.CryptProtectData(value, "Pengucro", None, None, None, 0)
            return protected[1] if isinstance(protected, tuple) else protected
        except Exception as exc:
            try:
                return cls._windows_dpapi(value, protect=True)
            except RuntimeError as fallback_exc:
                raise RuntimeError("보안 정보를 암호화할 수 없습니다.") from fallback_exc

    @classmethod
    def _unprotect(cls, value: bytes) -> bytes:
        try:
            import win32crypt
        except ImportError:
            return cls._windows_dpapi(value, protect=False)
        try:
            return win32crypt.CryptUnprotectData(value, None, None, None, 0)[1]
        except Exception as exc:
            try:
                return cls._windows_dpapi(value, protect=False)
            except RuntimeError as fallback_exc:
                raise RuntimeError("저장된 보안 정보를 복호화할 수 없습니다.") from fallback_exc

    def _load(self) -> dict[str, str]:
        raw = load_json(self.filename, {})
        return raw if isinstance(raw, dict) else {}

    def set(self, key: str, value: str) -> bool:
        try:
            if value:
                encrypted = self._protect(value.encode("utf-8"))
                encoded = base64.b64encode(encrypted).decode("ascii")
            else:
                encoded = ""

            def update(raw: Any) -> dict[str, str]:
                values = dict(raw) if isinstance(raw, dict) else {}
                if encoded:
                    values[key] = encoded
                else:
                    values.pop(key, None)
                return values

            update_json(self.filename, update, {})
            return True
        except (OSError, RuntimeError, TimeoutError, ValueError):
            # A secret backend problem must not prevent ReservationForm from
            # continuing to save config.json (people, site, threads, etc.).
            return False

    def get_or_set(self, key: str, value: str) -> tuple[str, bool]:
        """Atomically keep an existing secret or store the supplied fallback."""

        if not value:
            return self.get(key), False
        try:
            encrypted = self._protect(value.encode("utf-8"))
            candidate = base64.b64encode(encrypted).decode("ascii")
            winner = {"encoded": "", "inserted": False}

            def update(raw: Any) -> dict[str, str]:
                values = dict(raw) if isinstance(raw, dict) else {}
                existing = str(values.get(key, "") or "")
                if existing:
                    winner["encoded"] = existing
                else:
                    values[key] = candidate
                    winner["encoded"] = candidate
                    winner["inserted"] = True
                return values

            update_json(self.filename, update, {})
            if winner["inserted"]:
                return value, True
            decoded = base64.b64decode(winner["encoded"])
            return self._unprotect(decoded).decode("utf-8"), True
        except (OSError, RuntimeError, TimeoutError, UnicodeDecodeError, ValueError):
            return "", False

    def compare_and_set(
        self, key: str, expected_value: str, new_value: str
    ) -> tuple[str, bool]:
        """Change one secret only when its decrypted value still matches."""

        try:
            if new_value:
                encrypted = self._protect(new_value.encode("utf-8"))
                candidate = base64.b64encode(encrypted).decode("ascii")
            else:
                candidate = ""
            winner = {"value": ""}

            def update(raw: Any) -> dict[str, str]:
                values = dict(raw) if isinstance(raw, dict) else {}
                encoded = str(values.get(key, "") or "")
                if encoded:
                    current = self._unprotect(base64.b64decode(encoded)).decode("utf-8")
                else:
                    current = ""
                if current != expected_value:
                    winner["value"] = current
                    return values
                if candidate:
                    values[key] = candidate
                else:
                    values.pop(key, None)
                winner["value"] = new_value
                return values

            update_json(self.filename, update, {})
            return winner["value"], True
        except (OSError, RuntimeError, TimeoutError, UnicodeDecodeError, ValueError):
            return "", False

    def get(self, key: str, default: str = "") -> str:
        encoded = self._load().get(key)
        if not encoded:
            return default
        try:
            encrypted = base64.b64decode(encoded)
            return self._unprotect(encrypted).decode("utf-8")
        except (ValueError, RuntimeError, UnicodeDecodeError):
            return default

    def delete(self, key: str) -> bool:
        return self.set(key, "")
