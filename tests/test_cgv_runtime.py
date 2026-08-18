from engines.cgv_engine_guarded import CgvEngine as GuardedCgvEngine
from engines.cgv_engine_runtime import CgvEngine
from engines.registry import EngineRegistry


def _engine(logs=None):
    logs = logs if logs is not None else []
    return CgvEngine(lambda message, level: logs.append((message, level)))


def test_runtime_preserves_guarded_fast_seat_policy():
    assert issubclass(CgvEngine, GuardedCgvEngine)
    assert CgvEngine.FAST_SEAT_MAX_INFLIGHT == 1
    assert CgvEngine.FAST_SEAT_LAUNCH_INTERVAL_MS == 350


def test_preopen_schedule_watch_uses_subsecond_bounded_cadence():
    assert 0.5 <= CgvEngine.PREOPEN_IDLE_INTERVAL <= 0.75
    assert 0.5 <= CgvEngine.SCHEDULE_HINT_INTERVAL <= 0.5


def test_registry_uses_final_cgv_runtime():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode="",
        payload={},
        custom_sites={},
        log_callback=lambda _message, _level: None,
        success_callback=None,
    )
    assert isinstance(engine, CgvEngine)


def test_select_visitors_can_handoff_on_captured_initial_seat_response(monkeypatch):
    logs = []
    engine = _engine(logs)
    engine._initial_seat_response = {
        "status": 200,
        "data": {"data": {"items": []}},
    }
    monkeypatch.setattr(
        engine,
        "_seat_modal_snapshot",
        lambda _page: {"modalOpen": True, "seatCount": 0},
    )

    class Page:
        wait_calls = 0

        @staticmethod
        def is_closed():
            return False

        def wait_for_timeout(self, _milliseconds):
            self.wait_calls += 1

    page = Page()
    assert engine._select_visitors(page, 2) is True
    assert page.wait_calls == 0
    assert any("DOM 렌더 완료를 기다리지 않고" in message for message, _ in logs)


def test_advance_clicks_intermediate_checkout_confirmation(monkeypatch):
    logs = []
    engine = _engine(logs)
    state = {"confirmation_clicked": False, "first_clicked": False}

    class Page:
        @staticmethod
        def wait_for_timeout(_milliseconds):
            return None

    page = Page()

    monkeypatch.setattr(
        engine,
        "_cgv_payment_methods_ready",
        lambda _page: state["confirmation_clicked"],
    )

    def click_first(_page, _timeout):
        state["first_clicked"] = True
        return True, "결제하기"

    monkeypatch.setattr(engine, "_wait_and_click_payment_button", click_first)

    def click_confirmation(_page):
        assert state["first_clicked"] is True
        state["confirmation_clicked"] = True
        return True

    monkeypatch.setattr(engine, "_click_checkout_confirmation", click_confirmation)

    assert engine._advance_to_cgv_payment_methods(page) is True
    assert state == {"confirmation_clicked": True, "first_clicked": True}
    assert any("결제 전 확인 안내 확인" in message for message, _level in logs)


def test_advance_does_not_click_confirmation_after_payment_page_is_ready(monkeypatch):
    engine = _engine()
    state = {"ready": False, "confirmation_calls": 0}

    class Page:
        @staticmethod
        def wait_for_timeout(_milliseconds):
            return None

    page = Page()

    def ready(_page):
        return state["ready"]

    def click_first(_page, _timeout):
        state["ready"] = True
        return True, "결제하기"

    def unexpected_confirmation(_page):
        state["confirmation_calls"] += 1
        return True

    monkeypatch.setattr(engine, "_cgv_payment_methods_ready", ready)
    monkeypatch.setattr(engine, "_wait_and_click_payment_button", click_first)
    monkeypatch.setattr(engine, "_click_checkout_confirmation", unexpected_confirmation)

    assert engine._advance_to_cgv_payment_methods(page) is True
    assert state["confirmation_calls"] == 0


def test_runtime_rewrites_base_preopen_log_cadence():
    logs = []
    engine = _engine(logs)

    engine.log("[CGV] 미오픈 대기 · 20초 간격으로 시간표 확인", "info")
    engine.log("[CGV] 목표 영화 선공개 감지 · 감시 간격 단축 (2초)", "warning")

    assert any("0.75초 간격으로 목표 날짜 시간표 확인" in message for message, _ in logs)
    assert any("감시 간격 0.5초로 단축" in message for message, _ in logs)
