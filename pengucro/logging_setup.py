"""Persistent application logging with defensive data redaction.

The UI deliberately keeps only a small, readable in-memory history.  This
module is the separate diagnostic trail: application, UI, and engine messages
are appended to a rotating file below the Pengucro data directory and survive
UI clears and application restarts.

Only already-redacted text is handed to :mod:`logging`, and the file formatter
redacts the completed line once more.  The second pass also covers exception
text and values interpolated by third-party loggers.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pengucro.storage import get_data_dir


LOG_FILENAME = "app.log"  # Legacy name retained for callers/tests that import it.
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
RETENTION_DAYS = 14
MAX_PROCESS_LOGS = 40
SUCCESS_LEVEL = 25

logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")

# 010-1234-5678 / 01012345678 / 010-123-4567 / 02-123-4567 and similar.
_PHONE_PATTERN = re.compile(r"(?<!\d)0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)")
_EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    re.IGNORECASE,
)
_AUTH_HEADER_PATTERN = re.compile(
    r"(?im)(?P<label>\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:)"
    r"[^\r\n]*"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"(?:\.[A-Za-z0-9_-]{8,})?(?![A-Za-z0-9_-])"
)

_SENSITIVE_FIELD = (
    r"api[_ -]?key|client[_ -]?key|secret|password|passwd|authorization|"
    r"access[_ -]?token|refresh[_ -]?token|token|csrf(?:[_ -]?token)?|"
    r"captcha(?:[_ -]?(?:token|response))?|g-recaptcha-response|"
    r"h-captcha-response|session(?:[_ -]?(?:id|key|token))?|"
    r"phpsessid|jsessionid|booking[_ -]?(?:number|no)|reservation[_ -]?(?:number|no)"
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    rf"(?i)(?P<prefix>(?:[\"']?(?:{_SENSITIVE_FIELD})[\"']?)\s*[:=]\s*)"
    r"(?P<quote>[\"']?)(?P<value>[^\"'\s,;}&<>]+)(?P=quote)"
)
_SENSITIVE_QUERY_PATTERN = re.compile(
    rf"(?i)(?P<prefix>[?&](?:{_SENSITIVE_FIELD})=)[^&#\s]+"
)
_COOKIE_VALUE_PATTERN = re.compile(
    r"(?i)(?P<prefix>\b(?:PHPSESSID|JSESSIONID|SESSIONID|NID_AUT|NID_SES)=)"
    r"[^;\s,]+"
)

_REDACTED = "[redacted]"
_secrets: set[str] = set()
_secrets_lock = threading.RLock()
_configure_lock = threading.Lock()
_configured = False
_configured_path: Path | None = None
_owned_handler: logging.Handler | None = None
_run_lock = threading.Lock()
_run_id = "startup"
_run_sequence = 0


def register_secret(value: Any) -> None:
    """Mark a literal personal/secret value as never-to-be-written to disk."""
    if value is None:
        return
    try:
        text = str(value).strip()
    except Exception:
        return
    if not text:
        return
    with _secrets_lock:
        _secrets.add(text)


def forget_secrets() -> None:
    """Forget runtime literals after a booking run; existing files stay safe."""
    with _secrets_lock:
        _secrets.clear()


def _normalise_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


_SENSITIVE_MAPPING_KEYS = {
    "name",
    "username",
    "fullname",
    "phone",
    "phonenumber",
    "mobile",
    "email",
    "apikey",
    "clientkey",
    "clientsecret",
    "secret",
    "password",
    "authorization",
    "cookie",
    "cookies",
    "token",
    "accesstoken",
    "refreshtoken",
    "csrftoken",
    "captchatoken",
    "captcharesponse",
    "grecaptcharesponse",
    "hcaptcharesponse",
    "session",
    "sessionid",
    "bookingnumber",
    "reservationnumber",
}

_SENSITIVE_MAPPING_SUFFIXES = (
    "phone",
    "phonenumber",
    "email",
    "apikey",
    "clientkey",
    "clientsecret",
    "password",
    "accesstoken",
    "refreshtoken",
    "csrftoken",
    "captchatoken",
    "captcharesponse",
    "sessionid",
    "bookingnumber",
    "reservationnumber",
)


def _is_sensitive_mapping_key(key: Any) -> bool:
    normalised = _normalise_key(key)
    return normalised in _SENSITIVE_MAPPING_KEYS or normalised.endswith(
        _SENSITIVE_MAPPING_SUFFIXES
    )


def register_sensitive_mapping(values: Mapping[str, Any] | None) -> None:
    """Register sensitive literals found in a potentially nested payload.

    Values are registered only when their key is known to contain personal or
    authentication data.  Public identifiers such as branch/theme/slot IDs stay
    visible because they are useful when diagnosing an engine.
    """
    if not values:
        return

    seen: set[int] = set()

    def visit(item: Any, *, sensitive: bool = False) -> None:
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen:
                return
            seen.add(identity)
            for key, value in item.items():
                visit(value, sensitive=sensitive or _is_sensitive_mapping_key(key))
            return
        if isinstance(item, (list, tuple, set, frozenset)):
            identity = id(item)
            if identity in seen:
                return
            seen.add(identity)
            for value in item:
                visit(value, sensitive=sensitive)
            return
        if sensitive:
            register_secret(item)

    visit(values)


def replace_run_secrets(values: Mapping[str, Any] | None) -> None:
    """Replace literals retained for the previous run with the new payload."""
    forget_secrets()
    register_sensitive_mapping(values)


def scrub(text: Any, *, extra_secrets: Iterable[Any] = ()) -> str:
    """Return diagnostic text with personal and authentication data removed."""
    if text is None:
        return ""
    result = str(text)
    if not result:
        return result

    with _secrets_lock:
        current = list(_secrets)
    current.extend(str(value).strip() for value in extra_secrets if value is not None)
    # Longest first avoids leaking the tail of overlapping values.
    unique_secrets = {value for value in current if value}
    for secret in sorted((value for value in unique_secrets if len(value) >= 2), key=len, reverse=True):
        result = result.replace(secret, _REDACTED)
    # Very short names are masked only as standalone field/token values; a raw
    # replace would destroy ordinary Korean text containing the same syllable.
    for secret in (value for value in unique_secrets if len(value) == 1):
        result = re.sub(
            rf"(?<![\w가-힣]){re.escape(secret)}(?![\w가-힣])",
            _REDACTED,
            result,
        )

    result = _AUTH_HEADER_PATTERN.sub(lambda match: f"{match.group('label')} {_REDACTED}", result)
    result = _BEARER_PATTERN.sub(f"Bearer {_REDACTED}", result)
    result = _JWT_PATTERN.sub(_REDACTED, result)
    result = _SENSITIVE_QUERY_PATTERN.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}", result
    )
    result = _COOKIE_VALUE_PATTERN.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}", result
    )
    result = _SENSITIVE_VALUE_PATTERN.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote')}{_REDACTED}{match.group('quote')}",
        result,
    )
    result = _PHONE_PATTERN.sub(_REDACTED, result)
    return _EMAIL_PATTERN.sub(_REDACTED, result)


class RedactingFilter(logging.Filter):
    """Compatibility filter that sanitises a record before other handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub(record.getMessage())
            record.args = ()
        except Exception:
            # Logging must never raise into a booking worker.
            return True
        return True


class RedactingFormatter(logging.Formatter):
    """Redact the fully formatted line, including exception trace text."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            return scrub(super().format(record))
        except Exception:
            # Preserve a useful, safe minimum even for malformed log records.
            return scrub(f"{record.levelname} {record.name} {record.msg}")


class RuntimeContextFilter(logging.Filter):
    """Attach a cheap process-wide booking-run identifier to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        with _run_lock:
            record.run_id = _run_id
        return True


class AsyncRotatingFileHandler(logging.Handler):
    """Format/redact immediately, then perform disk I/O on one writer thread."""

    _STOP = object()

    def __init__(self, target: logging.handlers.RotatingFileHandler) -> None:
        super().__init__(target.level)
        self._target = target
        self._queue: queue.Queue[str | object] = queue.Queue(maxsize=8192)
        self._close_lock = threading.Lock()
        self._closing = False
        self._dropped = 0
        self._writer = threading.Thread(
            target=self._write_loop,
            name="PengucroLogWriter",
            daemon=True,
        )
        self._writer.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Formatting here snapshots the current run ID and secret registry.
            # It is CPU-only; open/rotate/write/flush all happen in _write_loop.
            line = self.format(record)
            with self._close_lock:
                if self._closing:
                    return
                try:
                    self._queue.put_nowait(line)
                except queue.Full:
                    self._dropped += 1
        except Exception:
            self.handleError(record)

    def _write_line(self, line: str) -> None:
        record = logging.LogRecord(
            name="pengucro.writer",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=line,
            args=(),
            exc_info=None,
        )
        self._target.handle(record)

    def _write_loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                if self._dropped:
                    dropped = self._dropped
                    self._dropped = 0
                    self._write_line(
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} WARNING "
                        f"pengucro.logging [pid={os.getpid()} run={current_run_id()} "
                        f"thread=PengucroLogWriter] 로그 과다로 {dropped}개 항목을 건너뜀"
                    )
                self._write_line(str(item))
            except Exception:
                # The logging path must never crash or call logging recursively.
                pass
            finally:
                self._queue.task_done()

    def flush_pending(self, timeout: float = 5.0) -> bool:
        """Wait until queued records are written, bounded for safe shutdown."""
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        try:
            self._target.flush()
        except Exception:
            pass
        return self._queue.unfinished_tasks == 0

    def flush(self) -> None:
        # logging.shutdown calls close immediately afterwards; close performs
        # the bounded drain. Avoid blocking twice here.
        try:
            self._target.flush()
        except Exception:
            pass

    def close(self) -> None:
        with self._close_lock:
            if self._closing:
                return
            self._closing = True
            # Ensure a sentinel can always be queued without an unbounded wait.
            # Dropping only occurs during shutdown and never delays submission.
            while True:
                try:
                    self._queue.put_nowait(self._STOP)
                    break
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                        self._queue.task_done()
                    except queue.Empty:
                        continue
        try:
            self._writer.join(timeout=5.0)
            self._target.flush()
            self._target.close()
        except Exception:
            pass
        finally:
            super().close()


def log_directory(base_directory: Path | None = None) -> Path:
    directory = Path(base_directory) if base_directory is not None else get_data_dir() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def begin_run() -> str:
    """Start and return a new non-personal identifier for one booking run."""
    global _run_id, _run_sequence
    with _run_lock:
        _run_sequence += 1
        stamp = time.strftime("%Y%m%d-%H%M%S")
        _run_id = f"{stamp}-{_run_sequence}-{uuid.uuid4().hex[:4]}"
        return _run_id


def current_run_id() -> str:
    with _run_lock:
        return _run_id


_PROCESS_LOG_PATTERN = re.compile(
    r"^app-(?P<date>\d{8})-(?P<pid>\d+)\.log(?:\.\d+)?$",
    re.IGNORECASE,
)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, AttributeError, ValueError):
        return False


def _cleanup_old_logs(directory: Path, *, keep: Path) -> None:
    """Best-effort retention cleanup without touching another live process."""
    now = time.time()
    cutoff = now - RETENTION_DAYS * 24 * 60 * 60
    candidates: list[tuple[float, Path, int]] = []
    try:
        paths = list(directory.glob("app-*.log*"))
    except OSError:
        return

    for path in paths:
        match = _PROCESS_LOG_PATTERN.match(path.name)
        if not match or path == keep:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        candidates.append((modified, path, int(match.group("pid"))))

    # Delete by age first, then cap inactive historical process logs. Rotated
    # siblings share the same PID and are intentionally counted independently.
    survivors: list[tuple[float, Path, int]] = []
    for modified, path, pid in candidates:
        if modified < cutoff and not _process_is_alive(pid):
            try:
                path.unlink()
                continue
            except OSError:
                pass
        survivors.append((modified, path, pid))

    excess = max(0, len(survivors) + 1 - MAX_PROCESS_LOGS)
    for _modified, path, pid in sorted(survivors)[:excess]:
        if _process_is_alive(pid):
            continue
        try:
            path.unlink()
        except OSError:
            pass


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "success": SUCCESS_LEVEL,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }.get(str(level).lower(), logging.INFO)


def persist_log_line(source: str, message: Any, level: str | int = "info") -> None:
    """Persist one already-safe UI/engine line without blocking on setup errors."""
    safe_source = re.sub(r"[^A-Za-z0-9_.-]", "_", str(source or "unknown"))
    try:
        logging.getLogger(f"pengucro.runtime.{safe_source}").log(
            _coerce_level(level), scrub(message)
        )
    except Exception:
        # Diagnostics can never be allowed to break the reservation path.
        pass


def persist_ui_lines(lines: Iterable[tuple[Any, str | int]]) -> None:
    """Persist UI-originated lines. Engine callbacks should persist at source."""
    for message, level in lines:
        persist_log_line("ui", message, level)


def configure(level: int = logging.INFO, *, base_directory: Path | None = None) -> Path | None:
    """Install the rotating file handler exactly once and return its path."""
    global _configured, _configured_path, _owned_handler
    with _configure_lock:
        if _configured:
            return _configured_path

        root = logging.getLogger()
        root.setLevel(level)

        try:
            directory = log_directory(base_directory)
            path = directory / f"app-{time.strftime('%Y%m%d')}-{os.getpid()}.log"
            _cleanup_old_logs(directory, keep=path)
            target_handler = logging.handlers.RotatingFileHandler(
                path,
                mode="a",
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
                delay=True,
            )
        except OSError:
            # A read-only or unavailable data directory must not stop the app.
            _configured = True
            _configured_path = None
            return None

        target_handler.setLevel(level)
        target_handler.setFormatter(logging.Formatter("%(message)s"))
        handler = AsyncRotatingFileHandler(target_handler)
        handler.setLevel(level)
        handler.addFilter(RuntimeContextFilter())
        handler.setFormatter(
            RedactingFormatter(
                "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s "
                "[pid=%(process)d run=%(run_id)s thread=%(threadName)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)
        _owned_handler = handler
        _configured = True
        _configured_path = path

        # Third-party libraries are chatty at DEBUG and rotate useful records away.
        for noisy in ("urllib3", "asyncio", "PIL", "websockets"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        return path


def shutdown() -> None:
    """Flush and stop the asynchronous writer without closing other handlers."""
    global _configured, _configured_path, _owned_handler
    with _configure_lock:
        handler = _owned_handler
        _owned_handler = None
        _configured = False
        _configured_path = None
        if handler is not None:
            logging.getLogger().removeHandler(handler)
    if handler is not None:
        handler.close()


def _reset_configuration_for_tests() -> None:
    """Detach this module's handler; intentionally private and test-only."""
    global _run_id, _run_sequence
    shutdown()
    with _run_lock:
        _run_id = "startup"
        _run_sequence = 0
    forget_secrets()
