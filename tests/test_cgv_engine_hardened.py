from engines import browser_session
from engines.cgv_engine import CgvEngine as BaseCgvEngine
from engines.cgv_engine_hardened import CgvEngine
from engines.registry import EngineRegistry


def test_registry_uses_hardened_cgv_engine():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode="",
        payload={},
        custom_sites={},
        log_callback=lambda *_args: None,
        success_callback=None,
    )

    assert isinstance(engine, CgvEngine)
    assert isinstance(engine, BaseCgvEngine)


def test_checkout_uses_recovered_active_page(monkeypatch):
    used = []

    class Browser:
        def is_connected(self):
            return True

    class Context:
        browser = Browser()

    class Page:
        def __init__(self, closed=False):
            self._closed = closed
            self.context = Context()

        def is_closed(self):
            return self._closed

    stale = Page(closed=True)
    recovered = Page(closed=False)
    engine = CgvEngine(lambda *_args: None)
    engine._active_page = recovered

    def fake_checkout(_self, page, developer_mode=False):
        used.append((page, developer_mode))
        return True

    monkeypatch.setattr(BaseCgvEngine, "_proceed_naver_pay_checkout", fake_checkout)

    assert engine._proceed_naver_pay_checkout(stale, developer_mode=True) is True
    assert used == [(recovered, True)]


def test_schedule_poll_syncs_browser_and_context_handles(monkeypatch):
    class Browser:
        def is_connected(self):
            return True

    class Context:
        def __init__(self):
            self.browser = Browser()

    class Page:
        def __init__(self):
            self.context = Context()

        def is_closed(self):
            return False

    monkeypatch.setattr(
        BaseCgvEngine,
        "_race_schedule",
        lambda _self, _page, _url, _concurrency: {"ok": True, "status": 200},
    )

    page = Page()
    engine = CgvEngine(lambda *_args: None)
    result = engine._race_schedule(page, "https://cgv.co.kr/fake", 1)

    assert result["ok"] is True
    assert engine._active_page is page
    assert engine._context is page.context
    assert engine._browser is page.context.browser


def test_reconnect_restarts_dead_chrome_endpoint_with_same_slot(monkeypatch):
    released = []
    started = []

    class OldChrome:
        port = 9333
        endpoint = "http://127.0.0.1:9333"

        def release(self):
            released.append("old")

    class FreshChrome:
        port = 9333
        endpoint = "http://127.0.0.1:9333"

    class Page:
        url = "https://cgv.co.kr/"

        def is_closed(self):
            return False

        def on(self, *_args):
            pass

    class Context:
        def __init__(self):
            self.pages = [Page()]

        def new_page(self):
            page = Page()
            self.pages.append(page)
            return page

    class Browser:
        def __init__(self):
            self.contexts = [Context()]

        def is_connected(self):
            return True

    class Chromium:
        def connect_over_cdp(self, endpoint):
            started.append(endpoint)
            return Browser()

    class Playwright:
        chromium = Chromium()

    engine = CgvEngine(lambda *_args: None)
    engine._chrome = OldChrome()
    engine._playwright = Playwright()

    monkeypatch.setattr(BaseCgvEngine, "_reconnect_seat_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(BaseCgvEngine, "_enter_visitor_page", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(BaseCgvEngine, "_select_visitors", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(browser_session, "cdp_descriptor", lambda _port: None)
    monkeypatch.setattr(browser_session, "start_isolated", lambda **_kwargs: FreshChrome())
    monkeypatch.setattr(engine, "_release_browser_lease_when_closed", lambda _chrome: None)

    recovered = engine._reconnect_seat_session(
        {"siteNo": "0013", "scnYmd": "20260818"}, people=2
    )

    assert recovered is not None
    assert released == ["old"]
    assert started == ["http://127.0.0.1:9333"]
    assert engine._chrome.port == 9333
    assert engine._active_page is recovered


def test_normal_selected_seat_summary_is_not_conflict():
    clicks = []
    conflict_checks = []

    class Page:
        context = None

        def is_closed(self):
            return False

        def evaluate(self, script, *_args):
            if "clean(b.textContent) === '선택완료'" in script:
                clicks.append("submit")
                return True
            if "strongConflictPhrases" in script:
                conflict_checks.append("checked")
                # Normal UI text such as '선택하신 좌석 D8, D9' must not be
                # promoted to a conflict by the hardened phrase list.
                assert "선택하신 좌석" not in script
                assert "선택된 좌석이" not in script
                return False
            if "hasPaySection" in script:
                return True
            return False

        def wait_for_timeout(self, _ms):
            pass

    engine = CgvEngine(lambda *_args: None)
    assert engine._submit_seat_selection(Page()) is True
    assert clicks == ["submit"]
    assert conflict_checks == ["checked"]


def test_real_conflict_still_stops_submission():
    dismissed = []

    class Page:
        context = None

        def is_closed(self):
            return False

        def evaluate(self, script, *_args):
            if "clean(b.textContent) === '선택완료'" in script:
                return True
            if "strongConflictPhrases" in script:
                return True
            return False

        def wait_for_timeout(self, _ms):
            pass

    engine = CgvEngine(lambda *_args: None)
    engine._click_visible_by_text = (
        lambda _page, labels: dismissed.append(labels) or True
    )

    assert engine._submit_seat_selection(Page()) is False
    assert dismissed == [("확인", "닫기", "취소")]
