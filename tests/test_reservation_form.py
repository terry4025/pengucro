from types import SimpleNamespace

from pengucro.models import NAVER_MODE, STANDARD_MODE, TRIPCOM_MODE
from ui.reservation_form import ReservationForm


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class Widget(Value):
    def __init__(self, value=0):
        super().__init__(value)
        self.config = {}

    def configure(self, **kwargs):
        self.config.update(kwargs)

    def set(self, value):
        self.value = value


def test_naver_time_picker_uses_selected_item_without_requiring_branch(monkeypatch):
    import engines.time_slot_fetchers as fetchers
    import tkinter.messagebox as messagebox
    import ui.reservation_form as reservation_form

    item_url = "https://booking.naver.com/booking/12/bizes/1325520/items/6446475"
    captured = {}

    def fake_dialog(parent, loader, on_select):
        captured["parent"] = parent
        captured["loader"] = loader
        captured["on_select"] = on_select

    def fake_fetch(config, branch_id, theme_id, date_str):
        captured["query"] = (config, branch_id, theme_id, date_str)
        return ["loaded"]

    warnings = []
    monkeypatch.setattr(reservation_form, "TimePickerDialog", fake_dialog)
    monkeypatch.setattr(fetchers, "fetch_any_time_slots", fake_fetch)
    monkeypatch.setattr(messagebox, "showwarning", lambda *args, **kwargs: warnings.append(args))

    form = SimpleNamespace(
        engine_mode_btn=Value(NAVER_MODE),
        config={
            "engine_id": "naver",
            "url": "https://booking.naver.com/booking/12/bizes/1325520",
            "branches": {},
            "themes": {"1": {"버디": item_url}},
        },
        branch_var=Value(""),
        theme_var=Value("버디"),
        date_entry=Value("2026-08-09"),
        _theme_id_for_name=lambda *_args: "",
        _set_selected_time=lambda value: None,
    )

    ReservationForm._open_time_picker(form)

    assert warnings == []
    assert captured["loader"]() == ["loaded"]
    query_config, branch_id, theme_id, date_str = captured["query"]
    assert query_config["url"] == item_url
    assert branch_id == "1"
    assert theme_id == item_url
    assert date_str == "2026-08-09"


def test_dpsnnn_thread_policy_limits_slider_to_four():
    slider = Widget(50)
    title = Widget()
    value_label = Widget()
    form = SimpleNamespace(
        engine_mode_btn=Value(STANDARD_MODE),
        dpsnnn_threads=50,
        standard_threads=30,
        keyescape_threads=1,
        threads_slider=slider,
        threads_title_label=title,
        threads_value_label=value_label,
        _site_uses_keyescape=lambda: False,
        _site_uses_dpsnnn=lambda: True,
    )

    ReservationForm._apply_thread_policy(form)

    assert slider.config["from_"] == 1
    assert slider.config["to"] == 4
    assert slider.config["number_of_steps"] == 3
    assert slider.value == 4
    assert form.dpsnnn_threads == 4
    assert "최대 4" in title.config["text"]


def test_tripcom_thread_policy_is_fixed_to_one():
    slider = Widget(30)
    title = Widget()
    value_label = Widget()
    form = SimpleNamespace(
        engine_mode_btn=Value(TRIPCOM_MODE),
        threads_slider=slider,
        threads_title_label=title,
        threads_value_label=value_label,
    )

    ReservationForm._apply_thread_policy(form)

    assert slider.value == 1
    assert slider.config["state"] == "disabled"
    assert "Trip.com" in title.config["text"]


def test_cgv_thread_policy_caps_slider_at_measured_safe_limit():
    slider = Widget(50)
    title = Widget()
    value_label = Widget()
    form = SimpleNamespace(
        engine_mode_btn=Value(STANDARD_MODE),
        cgv_threads=50,
        threads_slider=slider,
        threads_title_label=title,
        threads_value_label=value_label,
        _site_uses_keyescape=lambda: False,
        _site_uses_cgv=lambda: True,
        _site_uses_dpsnnn=lambda: False,
    )

    ReservationForm._apply_thread_policy(form)

    assert slider.config["to"] == 4
    assert slider.value == 4
    assert form.cgv_threads == 4
    assert "자동 감속" in title.config["text"]
