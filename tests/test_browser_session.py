import os
from pathlib import Path

from engines import browser_session


def test_isolated_slots_are_process_safe_and_reusable(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_session, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(browser_session, "is_port_open", lambda _port: False)

    first = browser_session.acquire_chrome_slot(slot_count=3)
    second = browser_session.acquire_chrome_slot(slot_count=3)
    try:
        assert first is not None and first.slot == 1
        assert second is not None and second.slot == 2
        assert first.port == browser_session.DEFAULT_CDP_PORT
        assert second.port == browser_session.DEFAULT_CDP_PORT + 1
        assert first.profile_path.name == "chrome-profile"
        assert second.profile_path.name == "chrome-profile-2"

        first.release()
        replacement = browser_session.acquire_chrome_slot(slot_count=3)
        try:
            assert replacement is not None and replacement.slot == 1
        finally:
            replacement.release()
    finally:
        first.release()
        second.release()


def test_stale_slot_lock_is_reclaimed(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_session, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(browser_session, "is_port_open", lambda _port: False)
    monkeypatch.setattr(browser_session, "_pid_alive", lambda _pid: False)
    lock_dir = tmp_path / browser_session.SESSION_LOCK_DIR_NAME
    lock_dir.mkdir(parents=True)
    (lock_dir / "slot-1.lock").write_text(str(os.getpid() + 99999), encoding="ascii")

    lease = browser_session.acquire_chrome_slot(slot_count=1)
    try:
        assert lease is not None and lease.slot == 1
    finally:
        lease.release()


def test_chrome_launch_failure_includes_port_and_exception_type(tmp_path, monkeypatch):
    messages = []
    monkeypatch.setattr(browser_session, "free_port", lambda port: port)
    monkeypatch.setattr(browser_session, "cdp_descriptor", lambda _port: None)
    monkeypatch.setattr(browser_session, "is_port_open", lambda _port: False)
    monkeypatch.setattr(browser_session, "find_chrome", lambda: Path("chrome.exe"))
    monkeypatch.setattr(
        browser_session.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    result = browser_session.start_or_attach(
        port=9444,
        log=lambda message, level="info": messages.append((message, level)),
        profile_path=tmp_path / "profile",
    )

    assert result is None
    assert "포트 9444" in messages[-1][0]
    assert "TimeoutError" in messages[-1][0]
    assert messages[-1][1] == "warning"


def test_pid_alive_returns_true_for_current_process():
    assert browser_session._pid_alive(os.getpid()) is True
    assert browser_session._pid_alive(0) is False
    assert browser_session._pid_alive(-1) is False


def test_pid_alive_handles_nonexistent_pid():
    assert browser_session._pid_alive(4194300) is False

