"""Minimal command-line bootstrap for the self-update hand-off.

This module intentionally imports neither CustomTkinter nor the main window.
``app.py`` can therefore dispatch an updater-helper copy before any GUI work,
then consume the private restart arguments while leaving unrelated user
arguments untouched.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pengucro.storage import get_data_dir


APPLY_UPDATE_FLAG = "--apply-update"
HEALTH_MARKER_FLAG = "--update-health-marker"
HEALTH_NONCE_FLAG = "--update-health-nonce"
UPDATED_FROM_FLAG = "--updated-from"
ROLLBACK_FROM_FLAG = "--update-rollback"

_PRIVATE_VALUE_FLAGS = {
    HEALTH_MARKER_FLAG: "health_marker",
    HEALTH_NONCE_FLAG: "health_nonce",
    UPDATED_FROM_FLAG: "updated_from",
    ROLLBACK_FROM_FLAG: "rollback_from",
}
_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_MARKER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.json$")


@dataclass(frozen=True)
class StartupUpdateContext:
    """Validated private restart data plus untouched application arguments."""

    remaining_args: tuple[str, ...]
    health_marker: Path | None = None
    health_nonce: str = ""
    updated_from: str = ""
    rollback_from: str = ""
    errors: tuple[str, ...] = ()

    @property
    def should_mark_ready(self) -> bool:
        return self.health_marker is not None and bool(self.health_nonce) and not self.errors


def dispatch_update_helper(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[[str | os.PathLike[str]], int] | None = None,
) -> int | None:
    """Dispatch ``--apply-update`` before importing any GUI modules.

    ``None`` means normal application startup.  If the private flag is present,
    the command must consist of exactly that flag and one absolute plan path;
    malformed helper invocations fail closed and never open the application.
    """

    arguments = list(sys.argv[1:] if argv is None else argv)
    if APPLY_UPDATE_FLAG not in arguments:
        return None

    # Import lazily: normal startup should pay no helper import cost, and app.py
    # can call this function before importing CustomTkinter/MainWindow.
    from pengucro.update_helper import EXIT_INVALID_PLAN, run_update_helper

    if len(arguments) != 2 or arguments[0] != APPLY_UPDATE_FLAG:
        return EXIT_INVALID_PLAN
    raw_plan_path = arguments[1]
    if not isinstance(raw_plan_path, str) or not raw_plan_path or "\x00" in raw_plan_path:
        return EXIT_INVALID_PLAN
    plan_path = Path(raw_plan_path).expanduser()
    if not plan_path.is_absolute():
        return EXIT_INVALID_PLAN
    return int((runner or run_update_helper)(plan_path))


def _split_private_arguments(arguments: Sequence[str]) -> tuple[dict[str, str], list[str], list[str]]:
    values: dict[str, str] = {}
    remaining: list[str] = []
    errors: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            remaining.extend(arguments[index:])
            break

        matched_flag = ""
        inline_value: str | None = None
        for flag in _PRIVATE_VALUE_FLAGS:
            if argument == flag:
                matched_flag = flag
                break
            prefix = f"{flag}="
            if argument.startswith(prefix):
                matched_flag = flag
                inline_value = argument[len(prefix) :]
                break
        if not matched_flag:
            remaining.append(argument)
            index += 1
            continue

        field = _PRIVATE_VALUE_FLAGS[matched_flag]
        if field in values:
            errors.append(f"duplicate:{matched_flag}")
        if inline_value is not None:
            value = inline_value
            index += 1
        elif index + 1 < len(arguments) and not arguments[index + 1].startswith("--"):
            value = arguments[index + 1]
            index += 2
        else:
            value = ""
            errors.append(f"missing:{matched_flag}")
            index += 1
        # Keep the first occurrence. A duplicate cannot silently override the
        # nonce or destination chosen by the updater helper.
        values.setdefault(field, value)
    return values, remaining, errors


def _health_root(data_directory: str | os.PathLike[str] | None) -> Path:
    base = Path(data_directory).expanduser() if data_directory is not None else get_data_dir()
    # Deliberately do not resolve the controlled suffix: validation compares a
    # candidate's physical parent with this lexical location, so a junction or
    # symlink inserted at ``updates``/``health`` is rejected rather than
    # followed outside the application data directory.
    return base.resolve(strict=False) / "updates" / "health"


def _validate_health_marker(
    raw_path: str,
    *,
    data_directory: str | os.PathLike[str] | None,
) -> Path | None:
    if not raw_path or "\x00" in raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute() or candidate.suffix.lower() != ".json":
        return None
    if candidate.is_symlink():
        return None
    resolved = candidate.resolve(strict=False)
    root = _health_root(data_directory)
    if resolved.parent != root or not _MARKER_NAME_PATTERN.fullmatch(resolved.name):
        return None
    return resolved


def consume_startup_update_args(
    argv: Sequence[str] | None = None,
    *,
    data_directory: str | os.PathLike[str] | None = None,
    mutate_sys_argv: bool | None = None,
) -> StartupUpdateContext:
    """Remove and validate updater-only restart flags.

    Unknown arguments are preserved byte-for-byte and in their original order.
    When called without an explicit ``argv``, the private flags are removed from
    ``sys.argv`` by default so GUI/toolkit consumers never see them.
    """

    implicit_argv = argv is None
    arguments = list(sys.argv[1:] if implicit_argv else argv)
    values, remaining, errors = _split_private_arguments(arguments)

    marker_raw = values.get("health_marker", "")
    nonce_raw = values.get("health_nonce", "")
    marker = _validate_health_marker(marker_raw, data_directory=data_directory) if marker_raw else None
    nonce = nonce_raw if _NONCE_PATTERN.fullmatch(nonce_raw) else ""
    if marker_raw and marker is None:
        errors.append("invalid:health-marker")
    if nonce_raw and not nonce:
        errors.append("invalid:health-nonce")
    if bool(marker_raw) != bool(nonce_raw):
        errors.append("incomplete:health-pair")
    if marker is None or not nonce:
        marker = None
        nonce = ""

    updated_from_raw = values.get("updated_from", "")
    rollback_from_raw = values.get("rollback_from", "")
    updated_from = updated_from_raw if _VERSION_PATTERN.fullmatch(updated_from_raw) else ""
    rollback_from = rollback_from_raw if _VERSION_PATTERN.fullmatch(rollback_from_raw) else ""
    if updated_from_raw and not updated_from:
        errors.append("invalid:updated-from")
    if rollback_from_raw and not rollback_from:
        errors.append("invalid:rollback-from")
    if updated_from and rollback_from:
        errors.append("conflict:update-result")
        updated_from = ""
        rollback_from = ""

    context = StartupUpdateContext(
        remaining_args=tuple(remaining),
        health_marker=marker,
        health_nonce=nonce,
        updated_from=updated_from,
        rollback_from=rollback_from,
        errors=tuple(dict.fromkeys(errors)),
    )
    should_mutate = implicit_argv if mutate_sys_argv is None else bool(mutate_sys_argv)
    if should_mutate:
        sys.argv[:] = [sys.argv[0], *remaining]
    return context


def write_update_health_marker(
    context: StartupUpdateContext,
    *,
    data_directory: str | os.PathLike[str] | None = None,
) -> bool:
    """Atomically confirm that the restarted GUI reached its ready point."""

    if not context.should_mark_ready or context.health_marker is None:
        return False
    marker = _validate_health_marker(
        str(context.health_marker), data_directory=data_directory
    )
    if marker is None or not _NONCE_PATTERN.fullmatch(context.health_nonce):
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after mkdir so a path swapped to a junction/symlink before
        # the write cannot redirect the marker outside the controlled folder.
        if marker.parent.resolve(strict=True) != _health_root(data_directory):
            return False
        if marker.exists() and (marker.is_symlink() or not marker.is_file()):
            return False
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{marker.name}.", suffix=".tmp", dir=marker.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {"state": "ready", "nonce": context.health_nonce},
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, marker)
        except Exception:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            return False
        return True
    except OSError:
        return False
