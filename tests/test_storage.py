import json
import os
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace
from pathlib import Path

from pengucro.storage import (
    SecretStore,
    get_data_dir,
    load_json,
    save_json,
    update_json,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_atomic_json_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    save_json("config.json", {"site": "제로월드", "threads": 10})
    assert load_json("config.json", {}) == {"site": "제로월드", "threads": 10}


def test_data_directory_is_independent_of_version_install_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("PENGUCRO_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    old_version_dir = tmp_path / "v6.69"
    new_version_dir = tmp_path / "v6.70"
    old_version_dir.mkdir()
    new_version_dir.mkdir()

    monkeypatch.chdir(old_version_dir)
    old_data_dir = get_data_dir()
    monkeypatch.chdir(new_version_dir)
    new_data_dir = get_data_dir()

    assert old_data_dir == new_data_dir == tmp_path / "local-app-data" / "Pengucro"


def test_update_json_preserves_existing_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    save_json(
        "config.json",
        {"site": "제로월드", "threads": 10, "remember_personal_info": True},
    )

    result = update_json(
        "config.json",
        lambda current: {**current, "threads": current["threads"] + 1},
        {},
    )

    assert result == tmp_path / "config.json"
    assert load_json("config.json", {}) == {
        "site": "제로월드",
        "threads": 11,
        "remember_personal_info": True,
    }


def test_update_json_is_safe_across_independent_processes(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    save_json("counter.json", {"count": 0, "marker": "preserved"})

    worker = textwrap.dedent(
        """
        import time

        from pengucro.storage import update_json

        def increment(current):
            time.sleep(0.003)
            return {**current, "count": current.get("count", 0) + 1}

        for _ in range(8):
            update_json("counter.json", increment, {"count": 0})
        """
    )
    environment = dict(os.environ)
    environment["PENGUCRO_DATA_DIR"] = str(tmp_path)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker],
            cwd=str(PROJECT_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]

    outputs = []
    for process in processes:
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise AssertionError(f"counter worker timed out: {stderr}")
        outputs.append((process.returncode, stdout, stderr))

    assert all(code == 0 for code, _stdout, stderr in outputs), outputs
    assert load_json("counter.json", {}) == {"count": 24, "marker": "preserved"}


def test_secret_store_set_preserves_other_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    barrier = threading.Barrier(2, timeout=5)

    def fake_protect(_cls, value):
        barrier.wait()
        return b"encrypted:" + value

    monkeypatch.setattr(SecretStore, "_protect", classmethod(fake_protect))
    store = SecretStore()
    results = []

    def set_value(key, value):
        results.append(store.set(key, value))

    threads = [
        threading.Thread(target=set_value, args=("first", "one")),
        threading.Thread(target=set_value, args=("second", "two")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == [True, True]
    raw = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert set(raw) == {"first", "second"}


def test_secret_store_get_or_set_keeps_one_winner_under_race(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    barrier = threading.Barrier(2, timeout=5)

    def fake_protect(_cls, value):
        barrier.wait()
        return b"encrypted:" + value

    def fake_unprotect(_cls, value):
        assert value.startswith(b"encrypted:")
        return value.removeprefix(b"encrypted:")

    monkeypatch.setattr(SecretStore, "_protect", classmethod(fake_protect))
    monkeypatch.setattr(SecretStore, "_unprotect", classmethod(fake_unprotect))
    store = SecretStore()
    results = []

    def migrate(value):
        results.append(store.get_or_set("yescaptcha_api_key", value))

    threads = [
        threading.Thread(target=migrate, args=("legacy-a",)),
        threading.Thread(target=migrate, args=("legacy-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert all(secret_backed for _value, secret_backed in results)
    assert len({value for value, _secret_backed in results}) == 1
    assert store.get("yescaptcha_api_key") == results[0][0]


def test_secret_store_compare_and_set_preserves_concurrent_value(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        SecretStore, "_protect", classmethod(lambda _cls, value: b"encrypted:" + value)
    )
    monkeypatch.setattr(
        SecretStore,
        "_unprotect",
        classmethod(lambda _cls, value: value.removeprefix(b"encrypted:")),
    )
    store = SecretStore()
    assert store.set("api", "concurrent-value") is True

    winner, persisted = store.compare_and_set(
        "api", "stale-loaded-value", "stale-edited-value"
    )

    assert persisted is True
    assert winner == "concurrent-value"
    assert store.get("api") == "concurrent-value"


def test_secret_store_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    store = SecretStore()
    store.set("api", "secret-value")
    assert store.get("api") == "secret-value"
    raw = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert "secret-value" not in raw["api"]
    store.delete("api")
    assert store.get("api") == ""


def test_pywin32_dpapi_error_falls_back_to_native_backend(monkeypatch):
    class Pywin32Error(Exception):
        pass

    fake_win32crypt = SimpleNamespace(
        CryptProtectData=lambda *_args: (_ for _ in ()).throw(Pywin32Error()),
        CryptUnprotectData=lambda *_args: (_ for _ in ()).throw(Pywin32Error()),
    )
    calls = []

    def fake_native(_cls, value, *, protect):
        calls.append(protect)
        return b"native:" + value if protect else value.removeprefix(b"native:")

    monkeypatch.setitem(sys.modules, "win32crypt", fake_win32crypt)
    monkeypatch.setattr(SecretStore, "_windows_dpapi", classmethod(fake_native))

    protected = SecretStore._protect(b"secret")
    assert protected == b"native:secret"
    assert SecretStore._unprotect(protected) == b"secret"
    assert calls == [True, False]


def test_naver_cookie_migration_removes_plaintext(monkeypatch, tmp_path):
    from engines.naver_engine import NaverEngine

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path / "appdata"))
    legacy = tmp_path / "naver_cookies.json"
    legacy.write_text('[{"name": "NID_SES", "value": "token"}]', encoding="utf-8")

    engine = NaverEngine(lambda *_args: None)
    cookies = engine._load_cookies()

    assert cookies[0]["name"] == "NID_SES"
    assert not legacy.exists()
    assert "token" not in (tmp_path / "appdata" / "secrets.json").read_text(encoding="utf-8")
