import asyncio
import json
import time

from engines.doomescape_engine import DoomEscapeEngine


def test_empty_timeout_error_has_a_useful_name():
    assert DoomEscapeEngine._describe_exception(asyncio.TimeoutError()) == "TimeoutError"


def test_exception_diagnostic_redacts_doom_completion_code():
    described = DoomEscapeEngine._describe_exception(
        RuntimeError("GET https://doomescape.com/rev?num=12&ck_code=SECRET-CODE failed")
    )

    assert "SECRET-CODE" not in described
    assert "ck_code=[redacted]" in described


def test_session_prefetch_runs_in_parallel(monkeypatch):
    import aiohttp

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def read(self):
            await asyncio.sleep(0.05)
            return b"ok"

    class FakeSession:
        def __init__(self, **_kwargs):
            self.closed = False

        def get(self, *_args, **_kwargs):
            return FakeResponse()

        async def close(self):
            self.closed = True

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    engine = DoomEscapeEngine("https://doomescape.com", lambda *_args: None)

    started = time.perf_counter()
    asyncio.run(engine.pre_fetch_sessions_async(5, {}))
    elapsed = time.perf_counter() - started

    assert len(engine.session_pool) == 5
    assert elapsed < 0.16


def test_outage_uses_one_probe_and_releases_all_workers(monkeypatch):
    logs = []
    engine = DoomEscapeEngine(
        "https://doomescape.com",
        lambda message, level: logs.append((message, level)),
    )
    probe_results = iter([False, True])

    async def fake_probe(_url):
        return next(probe_results)

    monkeypatch.setattr(engine, "_probe_reservation_page", fake_probe)
    engine.RECOVERY_INITIAL_SECONDS = 0
    engine.RECOVERY_MAX_SECONDS = 0

    async def run_waiters():
        engine._reset_async_recovery_state()
        await asyncio.gather(
            engine._wait_for_site_recovery("https://doomescape.com/list", 0, asyncio.TimeoutError()),
            engine._wait_for_site_recovery("https://doomescape.com/list", 1, asyncio.TimeoutError()),
        )

    asyncio.run(run_waiters())

    assert any("서버 응답 복구" in message for message, _level in logs)


def test_failure_diagnostic_persists_metadata_without_raw_html(tmp_path, monkeypatch):
    logs = []
    monkeypatch.chdir(tmp_path)
    engine = DoomEscapeEngine(
        "https://doomescape.com",
        lambda message, level: logs.append((message, level)),
    )
    raw = (
        '<html><input name="name" value="홍길동">'
        '<input name="phone" value="010-1234-5678">'
        '<input name="ck_code" value="SECRET-TOKEN">'
        '<meta http-equiv="refresh" content="0;url=rev.make.exe.php">완료</html>'
    )

    engine._write_safe_failure_summary(
        worker="태스크 2",
        stage="무통장 예약 결과 판정",
        status=200,
        response_text=raw,
        slot_id="34",
        order_id="9001",
    )

    target = tmp_path / "scratch" / "last_mutong_diagnostic.json"
    saved_text = target.read_text(encoding="utf-8")
    payload = json.loads(saved_text)

    assert payload["worker"] == "태스크 2"
    assert payload["http_status"] == 200
    assert payload["slot_id"] == "34"
    assert payload["order_id"] == "9001"
    assert payload["markers"]["has_completion_marker"] is True
    assert "홍길동" not in saved_text
    assert "010-1234-5678" not in saved_text
    assert "SECRET-TOKEN" not in saved_text
    assert "<html" not in saved_text
    assert any("민감정보를 제외한 요약" in message for message, _level in logs)


def test_http_diagnostic_includes_worker_stage_status_and_rtt():
    logs = []
    engine = DoomEscapeEngine(
        "https://doomescape.com",
        lambda message, level: logs.append((message, level)),
    )

    engine._log_http_diagnostic(
        "태스크 3", "예약 주문 생성", "POST", 503, 0.123, force=True
    )

    message, level = logs[-1]
    assert "[태스크 3]" in message
    assert "예약 주문 생성" in message
    assert "status=503" in message
    assert "RTT 123ms" in message
    assert level == "warning"
