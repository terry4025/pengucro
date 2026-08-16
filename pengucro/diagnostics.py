"""Safe helpers for engine diagnostics and opt-in debug snapshots."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pengucro.logging_setup import scrub


_HTML_TAG_PATTERN = re.compile(r"<(?:input|meta)\b[^>]*>", re.IGNORECASE | re.DOTALL)
_HTML_TEXTAREA_PATTERN = re.compile(
    r"(?P<open><textarea\b[^>]*>)(?P<body>.*?)(?P<close></textarea\s*>)",
    re.IGNORECASE | re.DOTALL,
)
_HTML_FIELD_NAME_PATTERN = re.compile(
    r"\b(?:name|id|property|http-equiv)\s*=\s*([\"'])"
    r"[^\"']*(?:name|phone|mobile|email|password|secret|token|captcha|session|"
    r"authorization|cookie|booking.?number|reservation.?number)[^\"']*\1",
    re.IGNORECASE,
)
_HTML_VALUE_PATTERN = re.compile(
    r"(?P<prefix>\b(?:value|content)\s*=\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)


def format_exception(exc: BaseException | None) -> str:
    """Return useful safe text even for exceptions whose ``str()`` is blank."""
    if exc is None:
        return "UnknownError"
    exception_type = type(exc).__name__ or "Exception"
    try:
        detail = scrub(str(exc)).strip()
    except Exception:
        detail = ""
    if detail:
        return f"{exception_type}: {detail}"

    # Some network exceptions expose useful errno/status attributes despite an
    # empty message.  Keep only primitive values to avoid serialising requests.
    attributes: list[str] = []
    for name in ("errno", "winerror", "status", "status_code"):
        try:
            value = getattr(exc, name, None)
        except Exception:
            continue
        if isinstance(value, (str, int)) and str(value).strip():
            attributes.append(f"{name}={scrub(value)}")
    if attributes:
        return f"{exception_type} ({', '.join(attributes)})"
    return exception_type


def redact_debug_text(content: Any, *, extra_secrets: Iterable[Any] = ()) -> str:
    """Redact HTML/JSON/plain-text diagnostics before they can reach disk."""
    text = "" if content is None else str(content)

    def redact_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        if not _HTML_FIELD_NAME_PATTERN.search(tag):
            return tag
        return _HTML_VALUE_PATTERN.sub(
            lambda value_match: (
                f"{value_match.group('prefix')}{value_match.group('quote')}"
                f"[redacted]{value_match.group('quote')}"
            ),
            tag,
        )

    def redact_textarea(match: re.Match[str]) -> str:
        opening = match.group("open")
        if not _HTML_FIELD_NAME_PATTERN.search(opening):
            return match.group(0)
        return f"{opening}[redacted]{match.group('close')}"

    text = _HTML_TAG_PATTERN.sub(redact_tag, text)
    text = _HTML_TEXTAREA_PATTERN.sub(redact_textarea, text)
    return scrub(text, extra_secrets=extra_secrets)


def write_redacted_debug_text(
    path: str | os.PathLike[str],
    content: Any,
    *,
    extra_secrets: Iterable[Any] = (),
    encoding: str = "utf-8",
) -> Path:
    """Atomically write a redacted debug snapshot and return its final path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_content = redact_debug_text(content, extra_secrets=extra_secrets)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{threading_id()}.tmp")
    try:
        temporary.write_text(safe_content, encoding=encoding)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def threading_id() -> int:
    # Local import keeps the module's hot import path small for engines that
    # only call ``format_exception``.
    import threading

    return threading.get_ident()
