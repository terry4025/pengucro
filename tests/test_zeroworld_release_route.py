"""Real UI dispatch/registry/base lifecycle; external requests are all mocked."""
import asyncio
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from engines import zeroworld_shin_engine as module
from engines.zeroworld_catalog import ZeroWorldTimeSlot
from pengucro.models import ReservationRequest, STANDARD_MODE
from ui.main_window import MainWindow


@pytest.mark.parametrize("outcome", ["success", "order_lost", "payment_lost"])
def test_mainwindow_registry_32_workers_submit_once(monkeypatch, tmp_path, outcome):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    row = dict(branch="5", themePK="9", themeLabel="시험 테마", reservationDate="2026-09-20",
               reservationTime="13:40", name="테스트", phone="01000000001", people=2)
    calls = []
    lookup = "테마 : 시험 테마 예약일시 : 2026년 9월 20일 13:40 인원 : 2명 진행상태 : 신청"
    class Response:
        status = 200
        headers = {}
        history = []
        url = "https://zero.example/response"
        def __init__(self, body="", lost=False): self.body, self.lost = body, lost
        async def __aenter__(self):
            if self.lost: raise asyncio.TimeoutError()
            return self
        async def __aexit__(self, *_): return False
        async def read(self): return self.body.encode("utf-8")
    class Session:
        async def close(self): pass
        def get(self, *_args, **_kwargs): return Response("<html>No final receipt</html>")
        def post(self, url, data, **_kwargs):
            action = data.get("act", "payment")
            calls.append(action)
            if action == "make":
                assert data["input_captcha"] == "12345"
                assert data["theme_num"] == "9" and data["rev_days"] == "2026-09-20"
                return Response('<form action="rev.make.mutong.php"><input name="code" value="CODE">'
                                '<input name="ck_code" value="12345"></form>', outcome == "order_lost")
            if action == "payment":
                return Response("예약번호 : 12345 " + lookup, outcome == "payment_lost")
            if action == "rev_view": return Response(lookup)
            return Response()
    async def prepare(engine, count, payload):
        assert count == 32 and payload["themeLabel"] == "시험 테마"
        engine.session_pool = [(Session(), True, "LIVE-ID") for _ in range(count)]
    monkeypatch.setattr(module.ZeroWorldShinEngine, "pre_fetch_sessions_async", prepare)
    monkeypatch.setattr(module.ZeroWorldShinEngine, "_prepare_captcha", AsyncMock(return_value="12345"))
    monkeypatch.setattr(module.ZeroWorldShinEngine, "_wait_for_date", AsyncMock(return_value=True))
    monkeypatch.setattr(module.ZeroWorldShinEngine, "_find_target_slot",
                        AsyncMock(return_value=ZeroWorldTimeSlot("13:40", "LIVE-ID", True)))
    monkeypatch.setattr(module.webbrowser, "open", Mock())
    form = Mock()
    form.developer_mode_enabled.return_value = False
    form.engine_mode_btn.get.return_value = STANDARD_MODE
    app = SimpleNamespace(
        _catalog_refresh_running=False, _keyescape_cache_running=False,
        site_var=SimpleNamespace(get=lambda: "제로월드"), form=form,
        log_panel=Mock(), site_dropdown=Mock(), add_site_btn=Mock(), delete_site_btn=Mock(),
        cta_btn=Mock(), _set_status_badge=Mock(), engine_event_queue=queue.Queue(), custom_sites={},
        _on_engine_log=Mock(), _on_booking_success=Mock(), _on_engine_status_update=Mock(),
        _on_engine_log_batch=Mock(), _update_booking_status=Mock(), _reset_cta_state=Mock())
    MainWindow._start_booking(app, ReservationRequest.from_mapping("제로월드", row), 32, True)
    engine = app.active_engine
    assert isinstance(engine, module.ZeroWorldShinEngine)
    engine.async_thread.join(timeout=5)
    assert not engine.async_thread.is_alive() and not engine.is_running
    assert calls.count("make") == 1
    assert calls.count("payment") == (0 if outcome == "order_lost" else 1)
    assert calls.count("rev_view") == (1 if outcome == "payment_lost" else 0)
    success = outcome != "order_lost"
    assert engine._final_submission_state == ("success" if success else "uncertain")
    assert engine._success_fired is success
    if success:
        app._on_booking_success.assert_called_once()
    else:
        app._on_booking_success.assert_not_called()
