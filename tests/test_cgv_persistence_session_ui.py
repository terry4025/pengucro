import base64
import json
from types import SimpleNamespace

import pytest

from engines.cgv_browser_client_runtime import CgvBrowserClient
from engines.cgv_engine_guarded import CgvEngine as GuardedCgvEngine
import engines.cgv_engine_runtime as cgv_engine_runtime
from engines.cgv_engine_runtime import CgvEngine, _MEMBER_SESSION_GUARD_ACTIVE
from pengucro import __release_sequence__, __version__
from pengucro.patch_notes import notes_for
from pengucro.storage import SecretStore, load_json, save_json
from ui.cgv_booking_dialog_runtime import CgvBookingDialog, parse_manual_seat_priorities


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


def test_broken_reused_page_is_discarded_before_retry():
    class Page:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    page = Page()
    CgvBrowserClient._discard_broken_page(page)
    assert page.close_calls == 1


def _jwt_with_exp(exp: float) -> str:
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'RS256'})}.{encode({'exp': exp})}.signature"


def test_opaque_member_session_is_reused_after_lightweight_auth_probe():
    logs = []
    engine = CgvEngine(lambda message, level: logs.append((message, level)))

    class Context:
        @staticmethod
        def cookies(_url):
            return [
                {"name": "accessToken", "value": "opaque-token"},
                {"name": "refresh_token", "value": "refresh-token"},
            ]

    class Page:
        @staticmethod
        def evaluate(script, argument):
            if "setInterval(run, intervalMs)" in script:
                assert argument["intervalMs"] == 60_000
                return {"unauthorized": False, "checkedAt": 0, "inFlight": True}
            assert "searchMblTktTabPrdtypList" in argument["url"]
            assert argument["accessToken"] == "opaque-token"
            assert "credentials: 'include'" in script
            # The official endpoint authenticates before validating custNo.
            return {
                "ok": False,
                "status": 400,
                "data": {"statusCode": 400, "statusMessage": "Bad Request"},
            }

    assert engine._ensure_member_session(Page(), Context()) is True
    assert any("로그인 세션 확인" in message for message, _level in logs)


def test_periodic_member_probe_is_installed_without_awaiting_network():
    observed = {}

    class Page:
        @staticmethod
        def evaluate(script, argument):
            observed["script"] = script
            observed["argument"] = argument
            return {"unauthorized": False, "checkedAt": 0, "inFlight": True}

    state = CgvEngine._install_member_session_guard(Page())

    assert state["unauthorized"] is False
    assert "const run = async () =>" in observed["script"]
    assert "run();" in observed["script"]
    assert "setInterval(run, intervalMs)" in observed["script"]
    assert observed["script"].lstrip().startswith("({url, intervalMs, authCodes})")
    assert observed["argument"]["intervalMs"] == 60_000


def test_nonmember_run_never_starts_member_mypage_probe(monkeypatch):
    events = []

    class Page:
        url = "https://cgv.co.kr/cnm/movieBook"

        @staticmethod
        def evaluate(*_args):
            raise AssertionError("nonmember schedule must not install member probe")

    page = Page()

    def fake_run(self, data):
        assert _MEMBER_SESSION_GUARD_ACTIVE.get() is False
        cgv = data["engine_metadata"]["cgv"]
        assert self._prepare_nonmember_session(page, cgv) is True
        return self._race_schedule(page, "https://cgv.co.kr/schedule", 1)

    monkeypatch.setattr(GuardedCgvEngine, "make_reservation_thread", fake_run)
    monkeypatch.setattr(
        GuardedCgvEngine,
        "_race_schedule",
        lambda _self, *_args: events.append("schedule") or {"ok": True},
    )

    engine = CgvEngine(lambda *_args: None)
    engine._prepare_nonmember_session = (
        lambda _page, _cgv: events.append("nonmember-auth") or True
    )
    result = engine.make_reservation_thread(
        {
            "people": 1,
            "engine_metadata": {
                "cgv": {"booking_mode": "비회원", "seat_groups": []}
            },
        }
    )

    assert result == {"ok": True}
    assert events == ["nonmember-auth", "schedule"]
    assert _MEMBER_SESSION_GUARD_ACTIVE.get() is True


def test_jwt_expiry_boundary_routes_only_stale_token_to_login(
    monkeypatch,
):
    now = 1_800_000_000.0
    fallback_calls = []
    monkeypatch.setattr(cgv_engine_runtime.time, "time", lambda: now)
    monkeypatch.setattr(
        GuardedCgvEngine,
        "_ensure_member_session",
        lambda _self, page, context: fallback_calls.append((page, context)) or True,
    )

    class Context:
        def __init__(self, token):
            self.token = token

        def cookies(self, _url):
            return [
                {"name": "accessToken", "value": self.token},
                {"name": "refresh_token", "value": "refresh-token"},
            ]

    class Page:
        @staticmethod
        def evaluate(*_args):
            raise AssertionError("parseable JWT freshness must not need a probe")

    engine = CgvEngine(lambda *_args: None)
    boundary = now + engine.MEMBER_SESSION_EXPIRY_LEEWAY_SECONDS
    stale_context = Context(_jwt_with_exp(boundary))
    fresh_context = Context(_jwt_with_exp(boundary + 1))
    stale_page = Page()

    assert engine._ensure_member_session(stale_page, stale_context) is True
    assert fallback_calls == [(stale_page, stale_context)]
    assert engine._ensure_member_session(Page(), fresh_context) is True
    assert fallback_calls == [(stale_page, stale_context)]


@pytest.mark.parametrize("status_code", (-1001, -1002))
def test_http_200_auth_error_payload_routes_to_login(
    monkeypatch,
    status_code,
):
    fallback_calls = []
    monkeypatch.setattr(
        GuardedCgvEngine,
        "_ensure_member_session",
        lambda _self, page, context: fallback_calls.append((page, context)) or True,
    )

    class Context:
        @staticmethod
        def cookies(_url):
            return [
                {"name": "accessToken", "value": "opaque-token"},
                {"name": "refresh_token", "value": "refresh-token"},
            ]

    class Page:
        @staticmethod
        def evaluate(_script, _argument):
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "statusCode": status_code,
                    "statusMessage": "인증 만료",
                },
            }

    page = Page()
    context = Context()
    engine = CgvEngine(lambda *_args: None)

    assert engine._ensure_member_session(page, context) is True
    assert fallback_calls == [(page, context)]


def test_secret_failure_does_not_abort_plain_config_persistence(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    store = SecretStore()

    def fail_protect(_value):
        raise RuntimeError("simulated dpapi packaging failure")

    monkeypatch.setattr(store, "_protect", fail_protect)
    assert store.set("reservation_name", "홍길동") is False

    save_json("config.json", {"people": "2", "remember_personal_info": True})
    assert load_json("config.json", {})["people"] == "2"


def test_ui_scopes_runtime_cgv_client_to_selector_only():
    import ui  # noqa: F401 - triggers runtime wiring
    from engines.cgv_browser_client import CgvBrowserClient as BaseClient
    from ui.cgv_booking_dialog import CgvBookingDialog as WiredDialog
    from ui.cgv_booking_dialog import CgvBrowserClient as DialogClient
    from ui.cgv_booking_dialog_runtime import CgvBookingDialog as RuntimeDialog

    assert issubclass(WiredDialog, RuntimeDialog)
    assert DialogClient is CgvBrowserClient
    assert BaseClient is not CgvBrowserClient
    assert BaseClient.__module__ == "engines.cgv_browser_client"


def test_seat_guide_visual_is_hidden_without_destroying_guide_state():
    class GuideFrame:
        def __init__(self):
            self.hidden = False

        def pack_forget(self):
            self.hidden = True

    guide = GuideFrame()
    guide_text = SimpleNamespace(master=guide)
    title = SimpleNamespace(master=guide_text)
    dialog = SimpleNamespace(
        guide_title_label=title,
        current_guide=object(),
        auto_seat_modes={"명당 자동 선택": "recommended"},
    )

    CgvBookingDialog._hide_visual_seat_guide(dialog)

    assert guide.hidden is True
    assert dialog.current_guide is not None
    assert "명당 자동 선택" in dialog.auto_seat_modes


def test_dialog_expands_height_to_show_more_seat_map():
    state = {"geometry": "1060x680+100+100", "minsize": None}

    def geometry(value=None):
        if value is None:
            return state["geometry"]
        state["geometry"] = value

    dialog = SimpleNamespace(
        update_idletasks=lambda: None,
        geometry=geometry,
        winfo_screenheight=lambda: 900,
        minsize=lambda width, height: state.__setitem__("minsize", (width, height)),
    )

    CgvBookingDialog._expand_dialog_for_seat_map(dialog)

    assert state["geometry"].startswith("1060x760+")
    assert state["minsize"] == (900, 560)


def test_cgv_npay_password_eye_toggle():
    class Entry:
        def __init__(self):
            self.show = "•"

        def configure(self, show):
            self.show = show

    class Button:
        def __init__(self):
            self.image = None

        def configure(self, image):
            self.image = image

    entry = Entry()
    btn = Button()
    form = SimpleNamespace(
        cgv_npay_password_entry=entry,
        cgv_npay_eye_button=btn,
        cgv_npay_eye_visible=False,
        _icon_eye=object(),
        _icon_eye_off=object(),
    )

    from ui.reservation_form import ReservationForm
    ReservationForm._toggle_cgv_npay_eye(form)
    assert form.cgv_npay_eye_visible is True
    assert entry.show == ""
    assert btn.image is form._icon_eye_off

    ReservationForm._toggle_cgv_npay_eye(form)
    assert form.cgv_npay_eye_visible is False
    assert entry.show == "•"
    assert btn.image is form._icon_eye


def test_cgv_npay_password_persistence_and_reservation_data(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    store = SecretStore()
    store.set("cgv_npay_password", "123456")
    assert store.get("cgv_npay_password") == "123456"

    store.delete("cgv_npay_password")
    assert store.get("cgv_npay_password") == ""


def test_v654_release_contract_is_complete():
    assert __version__ == "6.54"
    assert __release_sequence__ == 6540001
    note = notes_for("6.54")
    assert note is not None
    assert any("캐치테이블" in change for change in note.changes)
