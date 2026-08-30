"""Small cross-process rendezvous for Keyescape's public timetable response.

Only the public ``get_theme_time`` rows are shared.  Reservation forms,
personal data, cookies and captcha tokens remain private to each process.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from pengucro.storage import data_path


SHARE_DIR_NAME = "keyescape-slot-share"
CLOCK_DIR_NAME = "keyescape-clock-share"
RESULT_LIFETIME_SECONDS = 12.0
CLOCK_CLAIM_STALE_SECONDS = 8.0
ATOMIC_REPLACE_DELAYS = (0.0, 0.004, 0.012, 0.03)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil

        return bool(psutil.pid_exists(int(pid)))
    except Exception:
        pass
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        synchronize = 0x00100000
        query_limited = 0x1000
        handle = kernel32.OpenProcess(synchronize | query_limited, False, int(pid))
        if not handle:
            return int(ctypes.get_last_error()) == 5
        try:
            return int(kernel32.WaitForSingleObject(handle, 0)) == 0x00000102
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict) -> None:
    # Windows refuses ReplaceFile/MoveFileEx while another process happens to
    # have the destination open without delete sharing.  Server-clock readers
    # are intentionally lock-free, so retry the very small replacement window
    # instead of letting a non-essential sharing optimisation abort a booking.
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        last_error = None
        for delay in ATOMIC_REPLACE_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                os.replace(temporary, path)
                return
            except PermissionError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


class SharedSlotLookup:
    """Elect one local process to perform the first boundary timetable read."""

    def __init__(self, key: str, open_at: float):
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        root = data_path(SHARE_DIR_NAME)
        root.mkdir(parents=True, exist_ok=True)
        stamp = int(round(float(open_at)))
        self.prefix = root / f"{digest}-{stamp}"
        self.claim_path = self.prefix.with_suffix(".claim")
        self.started_path = self.prefix.with_suffix(".started")
        self.result_path = self.prefix.with_suffix(".result")
        self.open_at = float(open_at)
        self.owner = False

    def prepare(self) -> bool:
        """Atomically claim ownership, reclaiming only dead/stale owners."""
        for _attempt in range(2):
            try:
                handle = os.open(
                    self.claim_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                claim = _read_json(self.claim_path)
                pid = int(claim.get("pid") or 0)
                if _pid_alive(pid):
                    self.owner = pid == os.getpid()
                    return self.owner
                try:
                    self.claim_path.unlink()
                except OSError:
                    self.owner = False
                    return False
                continue
            try:
                payload = json.dumps(
                    {"pid": os.getpid(), "created": time.time()},
                    separators=(",", ":"),
                ).encode("utf-8")
                os.write(handle, payload)
            finally:
                os.close(handle)
            self.owner = True
            return True
        self.owner = False
        return False

    def mark_started(self) -> None:
        if self.owner:
            _atomic_json(
                self.started_path,
                {"pid": os.getpid(), "started": time.time()},
            )

    def publish(self, slots: list[dict]) -> None:
        if not self.owner or not slots:
            return
        _atomic_json(
            self.result_path,
            {
                "published": time.time(),
                # All local processes share the same (possibly inaccurate)
                # Windows wall clock, so a relative lifetime is safe even when
                # the PC clock differs from Keyescape's server clock.
                "expires": time.time() + RESULT_LIFETIME_SECONDS,
                "slots": slots,
            },
        )

    def wait_for_result(self, timeout: float = 1.5) -> list[dict]:
        """Wait briefly for the elected reader, returning only fresh rows."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        no_start_deadline = time.monotonic() + 0.08
        saw_started = False
        while time.monotonic() < deadline:
            result = _read_json(self.result_path)
            if result:
                expires = float(result.get("expires") or 0.0)
                slots = result.get("slots")
                if time.time() <= expires and isinstance(slots, list):
                    return [row for row in slots if isinstance(row, dict)]
            if not saw_started:
                saw_started = self.started_path.exists()
                # If the elected process never begins its read around T0, do not
                # make a healthy follower wait the full server-tail timeout.
                if (
                    not saw_started
                    and time.monotonic() >= no_start_deadline
                ):
                    return []
            time.sleep(0.003 if saw_started else 0.001)
        return []


class SharedServerClock:
    """Let concurrent Keyescape runs reuse one fresh server-clock pin."""

    def __init__(self, key: str):
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        root = data_path(CLOCK_DIR_NAME)
        root.mkdir(parents=True, exist_ok=True)
        self.state_path = root / f"{digest}.json"
        self.claim_path = root / f"{digest}.claim"

    def _apply_fresh(self, clock, max_age: float) -> bool:
        state = _read_json(self.state_path)
        snapshot = state.get("snapshot") if isinstance(state, dict) else None
        return bool(clock.apply_snapshot(snapshot or {}, max_age=max_age))

    def _try_claim(self) -> bool:
        for _attempt in range(2):
            try:
                handle = os.open(
                    self.claim_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                claim = _read_json(self.claim_path)
                pid = int(claim.get("pid") or 0)
                created = float(claim.get("created") or 0.0)
                stale = time.time() - created > CLOCK_CLAIM_STALE_SECONDS
                if _pid_alive(pid) and not stale:
                    return False
                try:
                    self.claim_path.unlink()
                except OSError:
                    return False
                continue
            except OSError:
                return False
            try:
                payload = json.dumps(
                    {"pid": os.getpid(), "created": time.time()},
                    separators=(",", ":"),
                ).encode("utf-8")
                os.write(handle, payload)
            finally:
                os.close(handle)
            return True
        return False

    def sync(
        self,
        clock,
        *,
        announce: bool = False,
        max_age: float = 5.0,
        wait_timeout: float = 3.0,
    ) -> bool:
        if self._apply_fresh(clock, max_age):
            if announce:
                clock.announce_sync(shared=True)
            return True

        if self._try_claim():
            try:
                ok = bool(clock.sync(announce=announce))
                snapshot = clock.snapshot() if ok else {}
                if snapshot:
                    try:
                        _atomic_json(
                            self.state_path,
                            {"pid": os.getpid(), "snapshot": snapshot},
                        )
                    except OSError:
                        # The local clock is already synchronised. Sharing its
                        # snapshot is optional and must never fail the page.
                        pass
                return ok
            finally:
                try:
                    self.claim_path.unlink()
                except OSError:
                    pass

        deadline = time.monotonic() + max(0.0, float(wait_timeout))
        while time.monotonic() < deadline:
            if self._apply_fresh(clock, max_age):
                if announce:
                    clock.announce_sync(shared=True)
                return True
            if not self.claim_path.exists():
                break
            time.sleep(0.01)

        # A dead or unusually slow owner must never block a booking run.
        return bool(clock.sync(announce=announce))
