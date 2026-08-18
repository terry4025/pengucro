import pytest

from engines.cgv_browser_client_runtime import CgvBrowserClient
from engines.cgv_engine_runtime import CgvEngine
from pengucro.storage import SecretStore, load_json, save_json
from ui.cgv_booking_dialog_runtime import parse_manual_seat_priorities


def test_manual_seat_priorities_work_without_live_seat_map():
    assert parse_manual_seat_priorities("C8,C9 | D10 D11", 2) == [
        ("C8", "C9"),
        ("D10", "D11"),
    ]


def test_manual_seat_priorities_require_exact_contiguous_group():
    with pytest.raises(ValueError):
        parse_manual_seat_priorities("C8,C10", 2)
    with pytest.raises(ValueError):
        parse_manual_seat_priorities("C8", 2)


def test_browser_client_prefers_latest_existing_cgv_tab():
    class Page:
        def __init__(self, url, closed=False):
            self.url = url
            self._closed = closed

        def is_closed(self):
            return self._closed

    first = Page("https://www.cgv.co.kr/cnm/movieBook/movie")
    latest = Page("https://www.cgv.co.kr/cnm/selectVisitorCnt")
    context = type(
        "Context",
        (),
        {"pages": [Page("about:blank"), first, Page("https://www.cgv.co.kr/", True), latest]},
    )()

    client = CgvBrowserClient()
    assert client._pick_existing_cgv_page(context) is latest


def test_member_session_is_reused_without_login_navigation():
    logs = []
    engine = CgvEngine(lambda message, level: logs.append((message, level)))

    class Context:
        @staticmethod
        def cookies(_url):
            return [{"name": "accessToken", "value": "present"}]

    # Deliberately no goto/evaluate methods: this proves the fast session check
    # returns before the historical /mem/login navigation path is touched.
    page = object()
    assert engine._ensure_member_session(page, Context()) is True
    assert any("로그인 세션 확인" in message for message, _level in logs)


def test_secret_failure_does_not_abort_plain_config_persistence(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    store = SecretStore()

    def fail_protect(_value):
        raise RuntimeError("simulated dpapi packaging failure")

    monkeypatch.setattr(store, "_protect", fail_protect)
    assert store.set("reservation_name", "홍길동") is False

    # ReservationForm can continue to this ordinary config save even when the
    # secret backend fails. This is the path that preserves people/thread/etc.
    save_json("config.json", {"people": "2", "remember_personal_info": True})
    assert load_json("config.json", {})["people"] == "2"


def test_ui_module_exports_runtime_cgv_dialog():
    import ui  # noqa: F401 - triggers runtime wiring
    from ui.cgv_booking_dialog import CgvBookingDialog as WiredDialog
    from ui.cgv_booking_dialog_runtime import CgvBookingDialog as RuntimeDialog

    assert WiredDialog is RuntimeDialog
