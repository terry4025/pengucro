import json

from pengucro.storage import SecretStore, load_json, save_json


def test_atomic_json_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    save_json("config.json", {"site": "제로월드", "threads": 10})
    assert load_json("config.json", {}) == {"site": "제로월드", "threads": 10}


def test_secret_store_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    store = SecretStore()
    store.set("api", "secret-value")
    assert store.get("api") == "secret-value"
    raw = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert "secret-value" not in raw["api"]
    store.delete("api")
    assert store.get("api") == ""


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
