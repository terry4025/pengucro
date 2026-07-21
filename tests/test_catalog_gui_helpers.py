from datetime import date, timedelta

import ui.main_window as main_window_module
from ui.main_window import AddSiteDialog, MainWindow


class FakeEntry:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class FakeForm:
    def __init__(self, value):
        self.date_entry = FakeEntry(value)


def target_for(value):
    window = object.__new__(MainWindow)
    window.form = FakeForm(value)
    return window._catalog_target_date()


def test_catalog_target_date_replaces_invalid_or_past_dates():
    assert target_for("not-a-date") == date.today().isoformat()
    assert target_for((date.today() - timedelta(days=3)).isoformat()) == date.today().isoformat()


def test_catalog_target_date_keeps_valid_future_date():
    future = (date.today() + timedelta(days=10)).isoformat()
    assert target_for(future) == future


class FakeLabel:
    def configure(self, **_kwargs):
        return None


def test_new_site_displays_confidence_but_auto_registers(monkeypatch):
    dialog = object.__new__(AddSiteDialog)
    dialog._parsing_in_progress = True
    dialog.status_label = FakeLabel()
    saved = []
    closed = []
    dialog.success_callback = saved.append
    dialog._on_cancel = lambda: closed.append(True)
    shown = []
    monkeypatch.setattr(main_window_module.messagebox, "showinfo", lambda *args, **kwargs: shown.append(args))
    monkeypatch.setattr(
        main_window_module.messagebox,
        "askyesno",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("확인 질문을 사용하면 안 됩니다.")),
    )
    result = {
        "engine_id": "jigubyeol",
        "branches": {"본점": "1"},
        "themes": {"1": {"테마": "10"}},
        "detection": {"confidence": 60, "evidence": ["지문"]},
    }

    dialog._on_parse_success(result)

    assert saved == [result]
    assert closed == [True]
    assert shown and "신뢰도 참고 지표: 60%" in shown[0][1]
