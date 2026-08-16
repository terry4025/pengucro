"""Attach automation to a real Chrome instance over the DevTools protocol.

Why
---
Playwright's ``browser.new_context()`` hands out an incognito-grade profile: no
cookies, no history, no Google session. reCAPTCHA scores such a browser as a
first-time unknown visitor, which is why the checkbox escalates to an image
puzzle even though the very same site passes silently in the user's everyday
browser. Nothing here spoofs or evades the challenge; the point is to run in a
browser that genuinely *is* an ordinary, persistent, signed-in one, so its
reputation is earned rather than faked.

The Chrome 136 restriction
--------------------------
Since Chrome 136 the ``--remote-debugging-port`` switch is ignored unless it is
paired with a ``--user-data-dir`` pointing somewhere other than the standard
Chrome data directory. Attaching to the literal Default profile is therefore no
longer possible -- Chrome silently drops the switch and the HTTP endpoint answers
with an error. (Verified on the installed Chrome 150: something was already
listening on 9222 from an earlier attempt and ``/json/version`` returned an HTTP
error rather than a browser descriptor.)

So a dedicated profile directory is used instead. It persists between runs, which
is the part that matters: cookies, site history and -- once the user signs in
there a single time -- the Google session all accumulate in it, exactly like a
normal browser.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from pengucro.diagnostics import format_exception
from pengucro.storage import get_data_dir


logger = logging.getLogger(__name__)

# 9222 is the conventional port and is frequently already taken by a stale
# Chrome started with the now-ineffective switch combination.
DEFAULT_CDP_PORT = 9333
PROFILE_DIR_NAME = "chrome-profile"
SESSION_LOCK_DIR_NAME = "chrome-session-locks"
ISOLATED_SLOT_COUNT = 4
LAUNCH_TIMEOUT_SECONDS = 25.0


def chrome_candidates() -> list[Path]:
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    return [
        Path(root) / "Google/Chrome/Application/chrome.exe"
        for root in roots
        if root
    ]


def find_chrome() -> Path | None:
    for candidate in chrome_candidates():
        if candidate.exists():
            return candidate
    return None


def profile_dir(slot: int = 1) -> Path:
    suffix = "" if int(slot) <= 1 else f"-{int(slot)}"
    return get_data_dir() / f"{PROFILE_DIR_NAME}{suffix}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if handle:
                kernel32.CloseHandle(handle)
                return True
            # Access denied still proves that the process exists.
            return int(kernel32.GetLastError()) == 5
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


class ChromeSlotLease:
    """Cross-process ownership of one Chrome port/profile pair."""

    def __init__(self, slot: int, lock_path: Path):
        self.slot = int(slot)
        self.port = DEFAULT_CDP_PORT + self.slot - 1
        self.profile_path = profile_dir(self.slot)
        self.lock_path = lock_path
        self.pid = os.getpid()
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            owner = int(self.lock_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            owner = 0
        if owner != self.pid:
            return
        try:
            self.lock_path.unlink()
        except OSError:
            pass


def acquire_chrome_slot(slot_count: int = ISOLATED_SLOT_COUNT) -> ChromeSlotLease | None:
    """Atomically reserve a persistent Chrome slot for this program process."""
    lock_dir = get_data_dir() / SESSION_LOCK_DIR_NAME
    lock_dir.mkdir(parents=True, exist_ok=True)
    for slot in range(1, max(1, int(slot_count)) + 1):
        port = DEFAULT_CDP_PORT + slot - 1
        # Do not claim a slot whose port belongs to an unrelated application.
        if is_port_open(port) and not cdp_descriptor(port):
            continue
        lock_path = lock_dir / f"slot-{slot}.lock"
        for _attempt in range(2):
            try:
                handle = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    owner = int(lock_path.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    owner = 0
                if _pid_alive(owner):
                    break
                try:
                    lock_path.unlink()
                except OSError:
                    break
                continue
            try:
                os.write(handle, str(os.getpid()).encode("ascii"))
            finally:
                os.close(handle)
            return ChromeSlotLease(slot, lock_path)
    return None


def is_port_open(port: int, timeout: float = 0.4) -> bool:
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def cdp_descriptor(port: int, timeout: float = 1.5) -> dict | None:
    """Return the browser descriptor, or None if this is not a live endpoint."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as response:
            data = json.load(response)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return data if isinstance(data, dict) and data.get("webSocketDebuggerUrl") else None


def free_port(preferred: int) -> int:
    """Preferred port, or the next few, skipping anything already bound."""
    for offset in range(0, 8):
        port = preferred + offset
        if not is_port_open(port):
            return port
        if cdp_descriptor(port):
            # A usable endpoint we can simply reuse.
            return port
    return preferred


class ChromeSession:
    """A Chrome instance reachable over CDP, plus how it got there."""

    def __init__(
        self,
        endpoint: str,
        port: int,
        process,
        launched: bool,
        first_run: bool,
        *,
        profile_path: Path | None = None,
        lease: ChromeSlotLease | None = None,
    ):
        self.endpoint = endpoint
        self.port = port
        self.process = process
        self.launched = launched
        self.first_run = first_run
        self.profile_path = profile_path or profile_dir()
        self.lease = lease

    def close_if_launched(self) -> None:
        """Only terminate Chrome if this object started it."""
        if not self.launched or self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass

    def release(self) -> None:
        if self.lease is not None:
            self.lease.release()
            self.lease = None


def start_or_attach(
    port: int = DEFAULT_CDP_PORT,
    log=None,
    *,
    profile_path: Path | None = None,
    allow_port_fallback: bool = True,
    lease: ChromeSlotLease | None = None,
) -> ChromeSession | None:
    """Reuse a running CDP-enabled Chrome, or start one on a dedicated profile."""

    def emit(message, level="info"):
        if log:
            log(message, level)
        logger.info(message)

    if allow_port_fallback:
        port = free_port(port)

    existing = cdp_descriptor(port)
    if existing:
        emit(f"실행 중인 Chrome에 연결합니다. ({existing.get('Browser', 'Chrome')})")
        return ChromeSession(
            f"http://127.0.0.1:{port}", port, None,
            launched=False, first_run=False,
            profile_path=profile_path, lease=lease,
        )

    if is_port_open(port):
        emit(
            f"[경고] {port} 포트가 사용 중이지만 DevTools 응답이 없습니다. "
            "다른 포트로 시도합니다.",
            "warning",
        )
        if not allow_port_fallback:
            return None
        port = free_port(port + 1)

    executable = find_chrome()
    if executable is None:
        emit("[경고] Chrome 실행 파일을 찾지 못했습니다.", "warning")
        return None

    data_dir = profile_path or profile_dir()
    first_run = not data_dir.exists()
    data_dir.mkdir(parents=True, exist_ok=True)

    arguments = [
        str(executable),
        f"--remote-debugging-port={port}",
        # Mandatory since Chrome 136: the switch above is ignored without a
        # non-standard data directory.
        f"--user-data-dir={data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # There used to be a "--restore-last-session=false" here, meant to stop
        # Chrome reopening old tabs. It did the exact opposite. Chrome reads that
        # switch with HasSwitch(), so its presence forces a session restore no
        # matter what value is attached -- "=false" included. Every launch dragged
        # back whatever had been open in this profile before. The switch is simply
        # gone now; without it Chrome opens the URL below and nothing else.
        #
        # The bubble flag covers the other way old tabs come back: if the process
        # was killed rather than closed, Chrome offers to restore the session on
        # the next start. This profile is driven by automation, so that prompt has
        # nobody to answer it.
        "--hide-crash-restore-bubble",
        "about:blank",
    ]
    try:
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except Exception as exc:
        emit(
            f"[경고] Chrome 실행 실패 · 포트 {port} · "
            f"{format_exception(exc)}",
            "warning",
        )
        return None

    deadline = time.monotonic() + LAUNCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        descriptor = cdp_descriptor(port)
        if descriptor:
            emit(f"Chrome을 실행했습니다. ({descriptor.get('Browser', 'Chrome')})", "success")
            if first_run:
                emit(
                    "[경고] 이 브라우저 프로필은 처음 생성되었습니다. 열린 창에서 "
                    "Google 계정으로 한 번 로그인하고 잠시 사용해두면, 다음 실행부터 "
                    "자동등록방지가 체크만으로 통과될 가능성이 높아집니다.",
                    "warning",
                )
            return ChromeSession(
                f"http://127.0.0.1:{port}", port, process,
                launched=True, first_run=first_run,
                profile_path=data_dir, lease=lease,
            )
        if process.poll() is not None:
            emit(
                f"[경고] Chrome이 예상보다 빨리 종료되었습니다 · "
                f"종료 코드 {process.returncode} · 포트 {port}",
                "warning",
            )
            return None
        time.sleep(0.25)

    emit(
        f"[경고] Chrome DevTools 연결 시간이 초과되었습니다 · "
        f"{LAUNCH_TIMEOUT_SECONDS:.0f}초 · 포트 {port}",
        "warning",
    )
    try:
        process.terminate()
    except Exception:
        pass
    return None


def start_isolated(log=None, slot_count: int = ISOLATED_SLOT_COUNT) -> ChromeSession | None:
    """Start or attach using a slot no other Pengucro process currently owns."""

    def emit(message, level="info"):
        if log:
            log(message, level)
        logger.info(message)

    lease = acquire_chrome_slot(slot_count)
    if lease is None:
        emit(
            f"[경고] 독립 Chrome 슬롯 {slot_count}개가 모두 사용 중입니다.",
            "warning",
        )
        return None
    emit(
        f"독립 Chrome 슬롯 {lease.slot}번을 사용합니다. "
        f"(포트 {lease.port})",
        "info",
    )
    session = start_or_attach(
        lease.port,
        log,
        profile_path=lease.profile_path,
        allow_port_fallback=False,
        lease=lease,
    )
    if session is None:
        lease.release()
    return session
