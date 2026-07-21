from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


APP_DIR_NAME = "Pengucro"


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
    legacy = Path.cwd() / filename
    if not destination.exists() and legacy.exists() and legacy.resolve() != destination.resolve():
        try:
            shutil.copy2(legacy, destination)
        except OSError:
            pass
    return destination


def load_json(filename: str, default: Any) -> Any:
    path = migrate_legacy_file(filename)
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        return default


def save_json(filename: str, value: Any) -> Path:
    path = data_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return path


def append_history(record: dict[str, Any]) -> Path:
    path = data_path("reservation_history.jsonl")
    safe_record = {key: value for key, value in record.items() if key not in {"name", "phone", "cookies"}}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(safe_record, ensure_ascii=False) + "\n")
    return path


class SecretStore:
    """Stores secrets encrypted for the current Windows user with DPAPI."""

    def __init__(self, filename: str = "secrets.json") -> None:
        self.filename = filename

    @staticmethod
    def _protect(value: bytes) -> bytes:
        try:
            import win32crypt

            protected = win32crypt.CryptProtectData(value, "Pengucro", None, None, None, 0)
            return protected[1] if isinstance(protected, tuple) else protected
        except ImportError as exc:
            raise RuntimeError("Windows DPAPI(pywin32)를 사용할 수 없습니다.") from exc

    @staticmethod
    def _unprotect(value: bytes) -> bytes:
        try:
            import win32crypt

            return win32crypt.CryptUnprotectData(value, None, None, None, 0)[1]
        except (ImportError, OSError) as exc:
            raise RuntimeError("저장된 보안 정보를 복호화할 수 없습니다.") from exc

    def _load(self) -> dict[str, str]:
        raw = load_json(self.filename, {})
        return raw if isinstance(raw, dict) else {}

    def set(self, key: str, value: str) -> None:
        values = self._load()
        if value:
            encrypted = self._protect(value.encode("utf-8"))
            values[key] = base64.b64encode(encrypted).decode("ascii")
        else:
            values.pop(key, None)
        save_json(self.filename, values)

    def get(self, key: str, default: str = "") -> str:
        encoded = self._load().get(key)
        if not encoded:
            return default
        try:
            encrypted = base64.b64decode(encoded)
            return self._unprotect(encrypted).decode("utf-8")
        except (ValueError, RuntimeError, UnicodeDecodeError):
            return default

    def delete(self, key: str) -> None:
        self.set(key, "")
