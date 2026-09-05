from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from engines.dpsnnn_runtime import WarmCheckout
from engines.dpsnnn_engine import DPSNNN_BRANCHES


@pytest.mark.parametrize('success', [True, False])
def test_visible_browser_only_after_confirmed_receipt(monkeypatch, success):
    import playwright.sync_api
    calls = []
    page = SimpleNamespace(url='https://www.dpsnnn.com/shop_payment_complete/?order_no=TEST',
        goto=Mock(), is_closed=lambda: True)
    context = SimpleNamespace(set_default_timeout=Mock(), new_page=lambda: page,
                              storage_state=lambda: {'cookies': [], 'origins': []})
    browser = SimpleNamespace(new_context=lambda **kw: context, close=Mock(), is_connected=lambda: True)
    def launch(**kwargs):
        calls.append(kwargs['headless'])
        if not kwargs['headless']:
            assert completed.is_set()
        return browser
    class Playwright:
        chromium = SimpleNamespace(launch=launch)
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', Playwright)
    monkeypatch.setattr('engines.dpsnnn_runtime.browser_session.find_chrome', lambda: Path('chrome.exe'))
    warm = WarmCheckout(DPSNNN_BRANCHES['gangnam'], {'reservationDate':'2026-09-20'}, lambda *a: None, Event())
    completed = Event()
    result = []
    warm.jobs.put((lambda *a: (success, 'TEST' if success else 'unknown'), completed, result))
    warm._run()
    assert calls == ([True, False] if success else [True])
    assert warm.finished.is_set() and not warm.error
    assert page.goto.call_count == int(success)
