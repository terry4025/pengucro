"""Release checks exercise real dispatch while replacing external side effects."""
import json
import queue
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from engines import dpsnnn_engine as module
from engines.dpsnnn_orders import OrderJournal
from engines.zeroworld_catalog import ZeroWorldTimeSlot
from pengucro.models import ReservationRequest, STANDARD_MODE
from ui.main_window import MainWindow


@pytest.mark.parametrize("lost", [False, True])
def test_mainwindow_registry_base_worker_dispatch(monkeypatch, tmp_path, lost):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    row = dict(branch="gangnam", themePK="행복", reservationDate="2026-09-12",
               reservationTime="17:50:00", name="테스트", phone="01000000001", people=2)
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None)
    monkeypatch.setattr(module, "create_dpsnnn_session", lambda: SimpleNamespace(
        get=lambda *a, **k: response, close=lambda: None, cookies=[]))
    monkeypatch.setattr(module, "fetch_exact_dpsnnn_slots", lambda *a, **k:
                        [ZeroWorldTimeSlot("17:50", "42", True)])
    class Warm:
        error = ""
        native_slot = ""
        native_seen_at = 0
        def __init__(self, *args):
            self.ready = Event()
            self.finished = Event()
        def start(self): self.ready.set()
        def close(self): self.finished.set()
    monkeypatch.setattr(module, "WarmCheckout", Warm)
    monkeypatch.setattr(module.DpsnnnEngine, "_build_order_payload", lambda *a: {"prod_idx": "42"})
    calls = []
    def submit(*args):
        calls.append(1)
        if lost:
            raise module.requests.Timeout("response lost")
        return "ORDER", "SUCCESS"
    monkeypatch.setattr(module.DpsnnnEngine, "_add_order", submit)
    monkeypatch.setattr(module.DpsnnnEngine, "_complete_checkout", lambda *a: (True, "RECEIPT-42"))
    form = Mock()
    form.developer_mode_enabled.return_value = False
    form.engine_mode_btn.get.return_value = STANDARD_MODE
    app = SimpleNamespace(
        _catalog_refresh_running=False, _keyescape_cache_running=False,
        site_var=SimpleNamespace(get=lambda: "단편선"), form=form,
        log_panel=Mock(), site_dropdown=Mock(), add_site_btn=Mock(), delete_site_btn=Mock(),
        cta_btn=Mock(), _set_status_badge=Mock(), engine_event_queue=queue.Queue(),
        custom_sites={"단편선": {"engine_id": "dpsnnn", "base_url": "https://www.dpsnnn.com"}},
        _on_engine_log=Mock(), _on_booking_success=Mock(), _on_engine_status_update=Mock(),
        _on_engine_log_batch=Mock(), _update_booking_status=Mock(), _reset_cta_state=Mock())
    MainWindow._start_booking(app, ReservationRequest.from_mapping("단편선", row), 1, False)
    engine = app.active_engine
    assert isinstance(engine, module.DpsnnnEngine)
    for thread in engine.threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert calls == [1]
    assert engine._success_fired is (not lost)
    journal = OrderJournal(row)
    assert journal.read()["state"] == ("unknown" if lost else "received")
    if lost:
        assert not journal.claim()
    else:
        assert journal.read()["booking_number"] == "RECEIPT-42"
    engine.stop_reservation()


def test_batch_window_renders_without_starting_reservations(monkeypatch, tmp_path):
    import customtkinter as ctk
    from ui import dpsnnn_batch
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"reservations": [dict(
        branch="gangnam", themePK="행복", reservationDate="2026-09-12",
        reservationTime="17:50", name="테스트", phone="01000000001", people=2)]}), encoding="utf-8")
    monkeypatch.setattr(dpsnnn_batch, "DpsnnnEngine", Mock(side_effect=AssertionError("must wait for Start")))
    checked = []
    def mainloop(app):
        app.update()
        from pengucro import __version__
        assert __version__ in app.title()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        buttons = [w for w in descendants(app) if isinstance(w, ctk.CTkButton)]
        assert any(w.cget("text") == "1건 예약 감시 시작" for w in buttons)
        checked.append(True)
        app.destroy()
    monkeypatch.setattr(ctk.CTk, "mainloop", mainloop)
    assert dpsnnn_batch.run_plan_window(plan) == 0
    assert checked == [True]
