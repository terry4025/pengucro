from engines.cgv_engine_pairwise_observer import CgvEngine as ObserverCgvEngine
from engines.cgv_engine_visitor_dom_runtime import CgvEngine
from engines.cgv_engine_visitor_runtime import CgvEngine as VisitorCgvEngine
from engines.registry import EngineRegistry


class DummyPage:
    def __init__(self):
        self.url = "https://cgv.co.kr/cnm/selectVisitorCnt"
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = url


def test_final_runtime_refreshes_visitor_route_even_when_already_on_it(monkeypatch):
    engine = CgvEngine(lambda _message, _level="info": None)
    page = DummyPage()
    restored = []

    monkeypatch.setattr(engine, "_login_required", lambda _page: False)
    monkeypatch.setattr(
        engine,
        "_restore_visitor_query",
        lambda _page, payload: restored.append(dict(payload)) or True,
    )
    monkeypatch.setattr(
        VisitorCgvEngine,
        "_open_visitor_route",
        lambda _self, _page, _payload: True,
    )

    payload = {"scnsNo": "100", "scnSseq": "1"}
    assert engine._open_visitor_route(page, payload) is True
    assert restored == [payload]
    assert len(page.goto_calls) == 1
    assert page.goto_calls[0][0].endswith("/cnm/selectVisitorCnt")


def test_registry_keeps_login_dom_observer_and_adaptive_layers():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode="",
        payload={},
        custom_sites={},
        log_callback=lambda _message, _level="info": None,
        success_callback=None,
    )
    assert isinstance(engine, CgvEngine)
    assert isinstance(engine, VisitorCgvEngine)
    assert isinstance(engine, ObserverCgvEngine)
