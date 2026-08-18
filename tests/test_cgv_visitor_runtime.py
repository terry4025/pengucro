from engines.cgv_engine_pairwise_observer import CgvEngine as ObserverCgvEngine
from engines.cgv_engine_visitor_runtime import CgvEngine
from engines.registry import EngineRegistry


class DummyPage:
    def __init__(self, url="https://cgv.co.kr/cnm/selectVisitorCnt"):
        self.url = url
        self.waits = []

    def is_closed(self):
        return False

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


def _snapshot(*, selected=False, modal=False, seat_count=0):
    return {
        "path": "/cnm/selectVisitorCnt",
        "routeReady": True,
        "generalFound": True,
        "targetFound": True,
        "targetSelected": selected,
        "targetEnabled": True,
        "selectFound": True,
        "selectEnabled": selected,
        "modalOpen": modal,
        "seatCount": seat_count,
    }


def test_manual_login_logs_request_then_resumes(monkeypatch):
    logs = []
    engine = CgvEngine(lambda message, level="info": logs.append((message, level)))
    page = DummyPage("https://cgv.co.kr/mem/login")
    states = iter((True, True, False))

    monkeypatch.setattr(engine, "_login_required", lambda _page: next(states))
    monkeypatch.setattr(engine, "_visitor_wait", lambda _page, _ms=None: None)

    assert engine._wait_for_manual_login(page) is True
    assert any("로그인을 완료" in message for message, _level in logs)
    assert any("로그인 완료를 확인" in message for message, _level in logs)


def test_three_visitors_are_verified_before_seat_modal(monkeypatch):
    logs = []
    engine = CgvEngine(lambda message, level="info": logs.append((message, level)))
    page = DummyPage()
    state = {"selected": False, "modal": False, "count_clicks": 0, "submit_clicks": 0}

    monkeypatch.setattr(engine, "_login_required", lambda _page: False)
    monkeypatch.setattr(engine, "_visitor_wait", lambda _page, _ms=None: None)

    def snapshot(_page, people):
        assert people == 3
        return _snapshot(
            selected=state["selected"],
            modal=state["modal"],
            seat_count=3 if state["modal"] else 0,
        )

    def click_count(_page, people):
        assert people == 3
        state["count_clicks"] += 1
        state["selected"] = True
        return True

    def click_submit(_page):
        state["submit_clicks"] += 1
        state["modal"] = True
        return True

    monkeypatch.setattr(engine, "_visitor_ui_snapshot", snapshot)
    monkeypatch.setattr(engine, "_click_visitor_count", click_count)
    monkeypatch.setattr(engine, "_click_visitor_submit", click_submit)

    assert engine._select_visitors(page, 3) is True
    assert state["count_clicks"] == 1
    assert state["submit_clicks"] == 1
    assert any("일반 3명 선택 상태 확인" in message for message, _level in logs)
    assert any("좌석 모달 로드 완료" in message for message, _level in logs)


def test_login_redirect_during_visitor_selection_resumes_same_flow(monkeypatch):
    logs = []
    engine = CgvEngine(lambda message, level="info": logs.append((message, level)))
    page = DummyPage("https://cgv.co.kr/mem/login")
    state = {"login": True, "selected": False, "modal": False, "restored": 0}

    monkeypatch.setattr(engine, "_login_required", lambda _page: state["login"])
    monkeypatch.setattr(engine, "_visitor_wait", lambda _page, _ms=None: None)

    def wait_login(_page):
        state["login"] = False
        page.url = "https://cgv.co.kr/"
        return True

    def open_route(_page, _payload):
        state["restored"] += 1
        page.url = "https://cgv.co.kr/cnm/selectVisitorCnt"
        return True

    def snapshot(_page, _people):
        return _snapshot(
            selected=state["selected"],
            modal=state["modal"],
            seat_count=2 if state["modal"] else 0,
        )

    monkeypatch.setattr(engine, "_wait_for_manual_login", wait_login)
    monkeypatch.setattr(engine, "_open_visitor_route", open_route)
    monkeypatch.setattr(engine, "_visitor_ui_snapshot", snapshot)
    monkeypatch.setattr(
        engine,
        "_click_visitor_count",
        lambda _page, _people: state.__setitem__("selected", True) or True,
    )
    monkeypatch.setattr(
        engine,
        "_click_visitor_submit",
        lambda _page: state.__setitem__("modal", True) or True,
    )

    engine._visitor_query_payload = {"scnsNo": "100"}
    assert engine._select_visitors(page, 2) is True
    assert state["restored"] == 1


def test_missing_target_visitor_button_reports_specific_error(monkeypatch):
    logs = []
    engine = CgvEngine(lambda message, level="info": logs.append((message, level)))
    page = DummyPage()

    monkeypatch.setattr(engine, "_login_required", lambda _page: False)
    monkeypatch.setattr(engine, "_visitor_wait", lambda _page, _ms=None: None)
    monkeypatch.setattr(
        engine,
        "_visitor_ui_snapshot",
        lambda _page, _people: {
            "routeReady": True,
            "generalFound": True,
            "targetFound": False,
            "modalOpen": False,
            "seatCount": 0,
        },
    )

    assert engine._select_visitors(page, 3) is False
    assert any("3명 버튼을 찾지 못했습니다" in message for message, _level in logs)


def test_registry_uses_visitor_runtime_without_losing_observer_layer():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode="",
        payload={},
        custom_sites={},
        log_callback=lambda _message, _level="info": None,
        success_callback=None,
    )
    assert isinstance(engine, CgvEngine)
    assert isinstance(engine, ObserverCgvEngine)
