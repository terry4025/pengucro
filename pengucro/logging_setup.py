"""Application logging with personal-information redaction.

Before this module existed, ``pengucro.catalog`` and
``engines.catalog_providers`` emitted ``logging.warning`` / ``logging.info`` /
``logging.debug`` records while no handler was ever installed, so every one of
those diagnostics was discarded. Catalog refresh failures in particular were
impossible to investigate after the fact.

Records are written to ``<data dir>/logs/app.log`` with rotation. A filter
redacts phone numbers and any value explicitly registered as sensitive, so the
reservation name and phone number the user typed do not end up on disk.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import threading
from pathlib import Path

from pengucro.storage import get_data_dir


LOG_FILENAME = "app.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3

# 010-1234-5678 / 01012345678 / 010-123-4567 / 02-123-4567 and similar.
_PHONE_PATTERN = re.compile(r"\b0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}\b")
_REDACTED = "[redacted]"

_secrets: set[str] = set()
_secrets_lock = threading.Lock()
_configured = False


def register_secret(value: str) -> None:
    """Mark a literal value (e.g. the reservation name) as never-to-be-logged."""
    text = (value or "").strip()
    # Very short values would redact unrelated substrings.
    if len(text) < 2:
        return
    with _secrets_lock:
        _secrets.add(text)


def forget_secrets() -> None:
    with _secrets_lock:
        _secrets.clear()


def scrub(text: str) -> str:
    if not text:
        return text
    with _secrets_lock:
        current = sorted(_secrets, key=len, reverse=True)
    for secret in current:
        if secret in text:
            text = text.replace(secret, _REDACTED)
    return _PHONE_PATTERN.sub(_REDACTED, text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub(str(record.msg))
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {key: scrub(str(value)) for key, value in record.args.items()}
                else:
                    record.args = tuple(scrub(str(value)) for value in record.args)
        except Exception:
            # Logging must never raise into the caller.
            return True
        return True


def log_directory() -> Path:
    directory = get_data_dir() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def configure(level: int = logging.INFO) -> Path | None:
    """Install the rotating file handler exactly once. Returns the log path."""
    global _configured
    if _configured:
        return log_directory() / LOG_FILENAME

    root = logging.getLogger()
    root.setLevel(level)

    try:
        path = log_directory() / LOG_FILENAME
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
    except OSError:
        # A read-only or missing data directory must not stop the app.
        _configured = True
        return None

    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)

    # Third-party libraries are chatty at DEBUG and would rotate the file away.
    for noisy in ("urllib3", "asyncio", "PIL", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    return path
