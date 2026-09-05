from datetime import date, timedelta
from types import MethodType, SimpleNamespace

from pengucro.models import NAVER_MODE, STANDARD_MODE, TRIPCOM_MODE
from ui.reservation_form import (
    DOOMESCAPE_MAX_WORKERS,
    ZEROWORLD_JIGUBYEOL_MAX_WORKERS,
    ReservationForm,
    _bounded_int,
    _merge_config_migration,
    _merge_form_config,
    _persist_yescaptcha_secret,
    _remove_matching_yescaptcha_plaintext,
    _resolve_yescaptcha_secret,
)


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget(Value):
    def __init__(self, value=0):
        super().__init__(value)
        self.config = {}

    def configure(self, **kwargs):
        self.config.update(kwargs)

    def cget(self, key):
        return self.config.get(key, "")

    def set(self, value):
        self.value = value

    def delete(self, *args, **kwargs):
        pass

    def insert(self, *args, **kwargs):
        pass

    def grid(self, *args, **kwargs):
        pass

    def grid_remove(self):
        pass

    def grid_forget(self):
        pass


class EntryWidget(Widget):
    def delete(self, *args, **kwargs):
        self.value = ""

    def insert(self, _index, value):
        self.value = value


class ToggleWidget(Widget):
    def select(self):
        self.value = True

    def deselect(self):
        self.value = False


class FakeSecretStore:
    def __init__(self, values=None, *, writable=True):
        self.values = dict(values or {})
        self.writable = writable
        self.calls = []

    def get(self, key, default=""):
        return self.values.get(key, default)

    def set(self, key, value):
        self.calls.append((key, value))
        if not self.writable:
            return False
        if value:
            self.values[key] = value
        else:
            self.values.pop(key, None)
        return True

    def get_or_set(self, key, value):
        if key in self.values:
            return self.values[key], True
        if not self.set(key, value):
            return "", False
        return value, True

    def compare_and_set(self, key, expected_value, new_value):
        current = self.values.get(key, "")
        if not self.writable:
            return "", False
        if current != expected_value:
            return current, True
        if new_value:
            self.values[key] = new_value
        else:
            self.values.pop(key, None)
        return new_value, True


def test_yescaptcha_plaintext_key_migrates_to_secret_store():
    store = FakeSecretStore()

    key, secret_backed, remove_plaintext = _resolve_yescaptcha_secret(
        store, {"yescaptcha_client_key": " legacy-key "}
    )

    assert key == "legacy-key"
    assert secret_backed is True
    assert remove_plaintext == "legacy-key"
    assert store.values["yescaptcha_api_key"] == "legacy-key"


def test_yescaptcha_plaintext_key_survives_when_secret_backend_fails():
    store = FakeSecretStore(writable=False)

    key, secret_backed, remove_plaintext = _resolve_yescaptcha_secret(
        store, {"yescaptcha_client_key": "legacy-key"}
    )

    assert key == "legacy-key"
    assert secret_backed is False
    assert remove_plaintext is None


def test_existing_yescaptcha_secret_wins_and_requests_plaintext_cleanup():
    store = FakeSecretStore({"yescaptcha_api_key": "encrypted-key"})

    key, secret_backed, remove_plaintext = _resolve_yescaptcha_secret(
        store, {"yescaptcha_client_key": "encrypted-key"}
    )

    assert key == "encrypted-key"
    assert secret_backed is True
    assert remove_plaintext == "encrypted-key"
    assert store.calls == []


def test_newer_plaintext_fallback_replaces_older_secret_when_dpapi_recovers():
    store = FakeSecretStore({"yescaptcha_api_key": "older-secret"})

    key, secret_backed, remove_plaintext = _resolve_yescaptcha_secret(
        store, {"yescaptcha_client_key": "newer-fallback"}
    )

    assert key == "newer-fallback"
    assert secret_backed is True
    assert remove_plaintext == "newer-fallback"
    assert store.values["yescaptcha_api_key"] == "newer-fallback"


def test_yescaptcha_plaintext_cleanup_preserves_newer_concurrent_fallback():
    result = _remove_matching_yescaptcha_plaintext(
        {"yescaptcha_client_key": "newer-key", "people": "2"},
        "stale-key",
    )

    assert result == {"yescaptcha_client_key": "newer-key", "people": "2"}


def test_yescaptcha_migration_survives_restart_with_real_secret_store(
    monkeypatch, tmp_path
):
    from pengucro.storage import SecretStore, load_json, save_json, update_json

    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    legacy_key = "synthetic-migration-key"
    save_json("config.json", {"yescaptcha_client_key": legacy_key})
    store = SecretStore()

    key, secret_backed, stale_plaintext = _resolve_yescaptcha_secret(
        store, load_json("config.json", {})
    )
    update_json(
        "config.json",
        lambda current: _remove_matching_yescaptcha_plaintext(
            current, stale_plaintext or ""
        ),
        {},
    )

    assert key == legacy_key
    assert secret_backed is True
    assert "yescaptcha_client_key" not in load_json("config.json", {})
    restarted_key, restarted_backed, _stale = _resolve_yescaptcha_secret(
        SecretStore(), load_json("config.json", {})
    )
    assert restarted_key == legacy_key
    assert restarted_backed is True


def test_load_config_migrates_and_populates_yescaptcha_key_before_empty_return(
    monkeypatch,
):
    import ui.reservation_form as reservation_form

    store = FakeSecretStore()
    persisted = {}
    monkeypatch.setattr(
        reservation_form,
        "load_json",
        lambda *_args, **_kwargs: {"yescaptcha_client_key": "legacy-key"},
    )

    def fake_update(_filename, updater, default):
        persisted.update(
            updater({"yescaptcha_client_key": "legacy-key"} or default)
        )

    monkeypatch.setattr(reservation_form, "update_json", fake_update)
    form = SimpleNamespace(
        secret_store=store,
        yescaptcha_client_key_entry=EntryWidget(""),
        yescaptcha_checkbox=ToggleWidget(False),
        yescaptcha_test_mode_checkbox=ToggleWidget(False),
        yescaptcha_soft_id_entry=EntryWidget(""),
        name_entry=EntryWidget(""),
        phone_entry=EntryWidget(""),
        cgv_nonmember_birth_entry=EntryWidget(""),
        cgv_nonmember_phone_entry=EntryWidget(""),
        cgv_nonmember_password_entry=EntryWidget(""),
    )
    form._load_config_values = MethodType(ReservationForm._load_config_values, form)

    ReservationForm.load_config(form)

    assert form.yescaptcha_client_key_entry.get() == "legacy-key"
    assert store.values["yescaptcha_api_key"] == "legacy-key"
    assert "yescaptcha_client_key" not in persisted
    assert form._is_initializing is False


def test_stale_form_does_not_revert_newer_shared_yescaptcha_settings():
    result = _merge_form_config(
        {
            "people": "2",
            "yescaptcha_enabled": True,
            "yescaptcha_test_mode": True,
            "yescaptcha_soft_id": "new-soft-id",
            "yescaptcha_client_key": "legacy-plaintext",
        },
        {
            "people": "4",
            "yescaptcha_enabled": False,
            "yescaptcha_test_mode": False,
            "yescaptcha_soft_id": "old-soft-id",
        },
        {
            "people": "2",
            "yescaptcha_enabled": False,
            "yescaptcha_test_mode": False,
            "yescaptcha_soft_id": "old-soft-id",
        },
        remove_plaintext_yescaptcha_key="legacy-plaintext",
    )

    assert result["people"] == "4"
    assert result["yescaptcha_enabled"] is True
    assert result["yescaptcha_test_mode"] is True
    assert result["yescaptcha_soft_id"] == "new-soft-id"
    assert "yescaptcha_client_key" not in result


def test_explicit_yescaptcha_setting_change_is_merged():
    result = _merge_form_config(
        {"yescaptcha_enabled": False, "yescaptcha_soft_id": "old"},
        {
            "yescaptcha_enabled": True,
            "yescaptcha_test_mode": False,
            "yescaptcha_soft_id": "changed",
        },
        {
            "yescaptcha_enabled": False,
            "yescaptcha_test_mode": False,
            "yescaptcha_soft_id": "old",
        },
    )

    assert result["yescaptcha_enabled"] is True
    assert result["yescaptcha_soft_id"] == "changed"


def test_blank_stale_window_does_not_delete_later_plaintext_fallback():
    result = _merge_form_config(
        {"yescaptcha_client_key": "written-by-another-process"},
        {
            "yescaptcha_enabled": False,
            "yescaptcha_test_mode": False,
            "yescaptcha_soft_id": "default",
        },
        {
            "yescaptcha_enabled": False,
            "yescaptcha_test_mode": False,
            "yescaptcha_soft_id": "default",
        },
        remove_plaintext_yescaptcha_key="key-loaded-by-stale-window",
    )

    assert result["yescaptcha_client_key"] == "written-by-another-process"


def test_unchanged_plaintext_fallback_does_not_overwrite_newer_key():
    result = _merge_form_config(
        {"yescaptcha_client_key": "newer-key"},
        {},
        {},
        plaintext_yescaptcha_key="old-key",
        plaintext_yescaptcha_expected="old-key",
    )

    assert result["yescaptcha_client_key"] == "newer-key"


def test_stale_form_only_updates_fields_changed_since_its_own_load():
    result = _merge_form_config(
        {"people": "4", "theme": "theme-a", "date": "2026-09-01"},
        {"people": "2", "theme": "theme-b", "date": "2026-09-01"},
        {"people": "2", "theme": "theme-a", "date": "2026-09-01"},
    )

    assert result == {
        "people": "4",
        "theme": "theme-b",
        "date": "2026-09-01",
    }


def test_unchanged_stale_secret_field_does_not_overwrite_newer_value():
    store = FakeSecretStore({"api": "newer-value"})
    form = SimpleNamespace(
        secret_store=store,
        _secret_baseline={"api": "value-loaded-by-this-window"},
    )

    result = ReservationForm._persist_secret_if_changed(
        form, "api", "value-loaded-by-this-window"
    )

    assert result is True
    assert store.calls == []
    assert store.values["api"] == "newer-value"


def test_unbacked_stale_yescaptcha_window_adopts_newer_secret():
    store = FakeSecretStore({"yescaptcha_api_key": "newer-key"})

    winner, secret_backed, failed = _persist_yescaptcha_secret(
        store, "old-fallback", "old-fallback", False
    )

    assert winner == "newer-key"
    assert secret_backed is True
    assert failed is False
    assert store.values["yescaptcha_api_key"] == "newer-key"


def test_stale_explicit_secret_change_cannot_overwrite_concurrent_winner():
    store = FakeSecretStore({"yescaptcha_api_key": "concurrent-key"})

    winner, secret_backed, failed = _persist_yescaptcha_secret(
        store, "edited-key", "loaded-key", True
    )

    assert winner == "concurrent-key"
    assert secret_backed is True
    assert failed is False


def test_config_migration_does_not_overwrite_concurrent_change():
    result = _merge_config_migration(
        {"site": "newer-site", "name": "newer-name", "untouched": 1},
        {"site": "legacy-site", "name": "legacy-name", "untouched": 1},
        {"site": "normalized-site", "untouched": 1},
    )

    assert result["site"] == "newer-site"
    assert result["name"] == "newer-name"
    assert result["untouched"] == 1


def test_malformed_thread_count_uses_bounded_fallback():
    assert _bounded_int("not-a-number", 10, 1, 50) == 10
    assert _bounded_int("999", 10, 1, 50) == 50


def test_keyescape_cache_button_delegates_to_main_window():
    calls = []
    form = SimpleNamespace(
        master=SimpleNamespace(
            _refresh_all_keyescape_timetables=lambda: calls.append("refresh")
        )
    )

    ReservationForm._request_keyescape_cache_refresh(form)

    assert calls == ["refresh"]


def test_keyescape_cache_state_disables_button_while_busy():
    form = SimpleNamespace(
        _booking_running=False,
        _keyescape_cache_busy=False,
        keyescape_cache_status=Widget(),
        keyescape_cache_btn=Widget(),
    )

    ReservationForm.set_keyescape_cache_state(form, "12/180 · 저장 10", busy=True)

    assert form._keyescape_cache_busy is True
    assert form.keyescape_cache_status.config["text"] == "12/180 · 저장 10"
    assert form.keyescape_cache_btn.config["state"] == "disabled"

    ReservationForm.set_keyescape_cache_state(form, "저장 완료", busy=False)
    assert form.keyescape_cache_btn.config["state"] == "normal"



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


def test_dpsnnn_thread_policy_uses_measured_limit():
    from engines.dpsnnn_engine import DPSNNN_MAX_WORKERS
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
    assert slider.config["to"] == DPSNNN_MAX_WORKERS
    assert slider.config["number_of_steps"] == DPSNNN_MAX_WORKERS - 1
    assert slider.value == DPSNNN_MAX_WORKERS
    assert form.dpsnnn_threads == DPSNNN_MAX_WORKERS
    assert f"프로그램당 최대 {DPSNNN_MAX_WORKERS}" in title.config["text"]


def test_zeroworld_and_jigubyeol_thread_policy_caps_slider_at_32():
    for site_name in ("제로월드", "지구별방탈출"):
        slider = Widget(50)
        title = Widget()
        form = SimpleNamespace(
            current_site=site_name,
            custom_sites={},
            engine_mode_btn=Value(STANDARD_MODE),
            standard_threads=50,
            threads_slider=slider,
            threads_title_label=title,
            threads_value_label=Widget(),
            _site_uses_keyescape=lambda: False,
            _site_uses_cgv=lambda: False,
            _site_uses_dpsnnn=lambda: False,
        )

        ReservationForm._apply_thread_policy(form)

        assert slider.config["to"] == ZEROWORLD_JIGUBYEOL_MAX_WORKERS
        assert slider.value == 32
        assert form.standard_threads == 32
        assert "최대 32" in title.config["text"]


def test_doomescape_thread_policy_uses_measured_fastest_count():
    from engines.doomescape_engine import DoomEscapeEngine

    slider = Widget(50)
    title = Widget()
    form = SimpleNamespace(
        current_site="둠이스케이프",
        custom_sites={},
        engine_mode_btn=Value(STANDARD_MODE),
        standard_threads=50,
        threads_slider=slider,
        threads_title_label=title,
        threads_value_label=Widget(),
        _site_uses_keyescape=lambda: False,
        _site_uses_cgv=lambda: False,
        _site_uses_dpsnnn=lambda: False,
    )

    ReservationForm._apply_thread_policy(form)

    assert DOOMESCAPE_MAX_WORKERS == DoomEscapeEngine.MAX_SCAN_SESSIONS == 10
    assert slider.config["to"] == 10
    assert slider.value == 10
    assert form.standard_threads == 10
    assert "실측 최고 10" in title.config["text"]


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
    assert "최초 응답 재사용" in title.config["text"]
    assert "제한 시 브라우저 전환" in title.config["text"]


def test_cgv_selection_summary_renders_preferred_times_and_seats():
    summary_label = Widget()
    button = Widget()
    form = SimpleNamespace(
        cgv_selection={
            "site_no": "0013",
            "site_name": "CGV 용산아이파크몰",
            "movie": "오디세이",
            "auditorium": "IMAX관",
            "format": "IMAX LASER 2D",
            "date": "2026-08-26",
            "people": 2,
            "show_time": "14:00",
            "preferred_times": ["14:00", "17:30", "21:00"],
            "seats": "H22,H23 | G22,G23",
        },
        cgv_selection_summary=summary_label,
        cgv_selector_button=button,
    )

    ReservationForm._render_cgv_selection_summary(form)

    text = summary_label.config["text"]
    assert "CGV 용산아이파크몰" in text
    assert "오디세이" in text
    assert "14:00 → 17:30 → 21:00" in text
    assert "H22,H23" in text
    assert button.config["text"] == "변경"


def test_cgv_set_selection_synchronizes_form_fields():
    class MockEntry:
        def __init__(self, value=""):
            self.value = value

        def delete(self, _start, _end):
            self.value = ""

        def insert(self, _pos, text):
            self.value = str(text)

        def get(self):
            return self.value

    class MockVar:
        def __init__(self, value=""):
            self.value = value

        def set(self, value):
            self.value = value

        def get(self):
            return self.value

    date_entry = MockEntry()
    time_entry = MockEntry()
    people_entry = MockEntry()
    branch_var = MockVar()
    theme_var = MockVar()
    summary_label = Widget()
    button = Widget()

    form = SimpleNamespace(
        cgv_selection={},
        date_entry=date_entry,
        time_entry=time_entry,
        people_entry=people_entry,
        branch_var=branch_var,
        theme_var=theme_var,
        cgv_selection_summary=summary_label,
        cgv_selector_button=button,
        auto_save=lambda: None,
        _render_cgv_selection_summary=lambda: ReservationForm._render_cgv_selection_summary(form),
    )

    ReservationForm._set_cgv_selection(
        form,
        {
            "site_no": "0013",
            "site_name": "CGV 용산아이파크몰",
            "movie": "오디세이",
            "auditorium": "IMAX관",
            "date": "2026-08-26",
            "people": 2,
            "show_time": "14:00",
            "preferred_times": ["14:00", "17:30"],
            "seats": "H22,H23",
        },
    )

    assert date_entry.get() == "2026-08-26"
    assert time_entry.get() == "14:00"
    assert people_entry.get() == "2"
    assert branch_var.get() == "CGV 용산아이파크몰"
    assert theme_var.get() == "오디세이"


def test_cgv_dialog_people_change_revalidates_seat_priority_groups():
    from ui.cgv_booking_dialog import CgvBookingDialog

    dialog = SimpleNamespace(
        people=2,
        people_label=Widget(),
        priority_groups=[("H22", "H23"), ("G22", "G23")],
        current_seats={"H22", "H23"},
        seat_help=Widget(),
        seats=(),
        auto_seat_modes={},
        auto_seat_menu=Widget(),
        auto_seat_var=Widget(),
        _auto_seat_options=lambda: {},
        _render_seats=lambda: None,
        _render_priorities=lambda: None,
        _update_confirm_state=lambda: None,
    )

    # Change people from 2 to 3
    CgvBookingDialog._set_people(dialog, 3)

    assert dialog.people == 3
    # 2-seat groups are now invalid for 3 people, so they are purged
    assert dialog.priority_groups == []
    assert dialog.current_seats == set()
    assert "3석씩 선택" in dialog.seat_help.config["text"]


def test_cgv_calendar_button_resolves_date_picker_dialog(monkeypatch):
    import ui.cgv_booking_dialog as cgv_dialog_mod
    import ui.reservation_form as rf_mod
    from ui.cgv_booking_dialog import CgvBookingDialog

    captured = {}

    def fake_date_picker(parent, initial_date, on_select, **kwargs):
        captured["parent"] = parent
        captured["initial_date"] = initial_date
        captured["on_select"] = on_select

    monkeypatch.setattr(rf_mod, "DatePickerDialog", fake_date_picker)

    dialog = SimpleNamespace(
        reservation_date="2026-08-26",
        _change_date=lambda d: captured.update({"new_date": d}),
    )

    CgvBookingDialog._open_calendar_picker(dialog)

    assert captured["initial_date"] == "2026-08-26"
    assert callable(captured["on_select"])


def test_cgv_dialog_active_task_with_rapid_date_changes_queues_and_runs_newest_only():
    import queue
    import threading
    import time
    from ui.cgv_booking_dialog import CgvBookingDialog

    loaded_results = []
    status_label = Widget()

    dialog = SimpleNamespace(
        _closing=False,
        _next_task_id=0,
        _active_task_id=None,
        _active_task_done=None,
        _active_cancel_event=None,
        _pending_task=None,
        _ui_event_queue=queue.Queue(),
        _task_thread_local=threading.local(),
        _request_generation=1,
        status_label=status_label,
        winfo_exists=lambda: True,
        after=lambda ms, cb: None,
        _handle_task_error=lambda msg: None,
    )
    dialog._launch_task = lambda status, func, done: CgvBookingDialog._launch_task(dialog, status, func, done)
    dialog._start_task = lambda status, func, done: CgvBookingDialog._start_task(dialog, status, func, done)
    dialog._poll_task = lambda: CgvBookingDialog._poll_task(dialog)
    dialog._finish_active_task = lambda **kw: CgvBookingDialog._finish_active_task(dialog, **kw)

    # Task A starts
    dialog._start_task("조회 A", lambda ce: (time.sleep(0.02), "result_A")[1], lambda res: loaded_results.append(("A", res)))

    # User rapidly changes to B then C
    # Request B is submitted while A is active
    dialog._start_task(
        "조회 B",
        lambda ce: "result_B",
        lambda res: loaded_results.append(("B", res)),
    )
    assert dialog._pending_task is not None
    assert dialog._pending_task[0] == "조회 B"

    # Request C is submitted while A is still active
    dialog._request_generation = 3
    dialog._start_task(
        "조회 C",
        lambda ce: (time.sleep(0.02), "result_C")[1],
        lambda res: loaded_results.append(("C", res)),
    )
    assert dialog._pending_task[0] == "조회 C"

    # Wait for Task A to finish
    time.sleep(0.04)
    dialog._poll_task()

    # When A finishes and pending task C exists, pending task C starts immediately
    assert dialog._active_task_id == 2
    assert dialog._pending_task is None

    # Wait for Task C to finish
    time.sleep(0.04)
    dialog._poll_task()

    assert ("C", "result_C") in loaded_results
    # Request B was superseded by C and never executed
    assert not any(item[0] == "B" for item in loaded_results)


def test_cgv_dialog_site_change_while_active_task_runs_newest_site():
    import queue
    import threading
    import time
    from ui.cgv_booking_dialog import CgvBookingDialog

    loaded_sites = []
    dialog = SimpleNamespace(
        _closing=False,
        _next_task_id=0,
        _active_task_id=None,
        _active_task_done=None,
        _active_cancel_event=None,
        _pending_task=None,
        _ui_event_queue=queue.Queue(),
        _task_thread_local=threading.local(),
        _request_generation=1,
        status_label=Widget(),
        winfo_exists=lambda: True,
        after=lambda ms, cb: None,
        _handle_task_error=lambda msg: None,
    )
    dialog._launch_task = lambda status, func, done: CgvBookingDialog._launch_task(dialog, status, func, done)
    dialog._start_task = lambda status, func, done: CgvBookingDialog._start_task(dialog, status, func, done)
    dialog._poll_task = lambda: CgvBookingDialog._poll_task(dialog)
    dialog._finish_active_task = lambda **kw: CgvBookingDialog._finish_active_task(dialog, **kw)

    # Site 1 is currently loading
    dialog._start_task("지점 1 조회", lambda ce: (time.sleep(0.02), "snapshot_1")[1], lambda res: loaded_sites.append(("site_1", res)))

    # User clicks Site 2 while Site 1 is loading
    dialog._request_generation = 2
    dialog._start_task(
        "지점 2 조회",
        lambda ce: (time.sleep(0.02), "snapshot_2")[1],
        lambda res: loaded_sites.append(("site_2", res)),
    )
    assert dialog._pending_task is not None

    # Wait for Site 1 to finish
    time.sleep(0.04)
    dialog._poll_task()

    # Site 2 task is now active
    assert dialog._active_task_id == 2
    assert dialog._pending_task is None

    # Wait for Site 2 to finish
    time.sleep(0.04)
    dialog._poll_task()

    assert ("site_2", "snapshot_2") in loaded_sites


def test_cgv_mode_disables_main_form_date_time_people_controls():
    form = SimpleNamespace(
        engine_mode_btn=Value(STANDARD_MODE),
        _site_uses_cgv=lambda: True,
        _keyescape_ui_active=lambda: False,
        yescaptcha_enabled_var=Value(False),
        yescaptcha_test_mode_checkbox=Widget(),
        yescaptcha_frame=Widget(),
        cgv_auth_frame=Widget(),
        catalog_auto_refresh_checkbox=Widget(),
        threads_frame=Widget(),
        _apply_thread_policy=lambda: None,
        _on_cgv_booking_mode_change=lambda: None,
        _toggle_custom_theme=lambda: None,
        _update_dev_mode_state=lambda: None,
        current_site="CGV",
        show_server_time_checkbox=Widget(),
        branch_dropdown=Widget(),
        branch_label=Widget(),
        theme_dropdown=Widget(),
        theme_label=Widget(),
        date_entry=Widget(),
        date_label=Widget(),
        date_picker_btn=Widget(),
        time_entry=Widget(),
        time_label=Widget(),
        time_picker_btn=Widget(),
        people_entry=Widget(),
        people_label=Widget(),
        name_entry=Widget(),
        name_label=Widget(),
        phone_entry=Widget(),
        phone_label=Widget(),
        custom_theme_checkbox=Widget(),
        theme_pk_entry=Widget(),
        cgv_site_no_entry=Widget(),
        day_type_segmented=Widget(),
        day_type_label=Widget(),
        cgv_movie_entry=Widget(),
        cgv_auditorium_entry=Widget(),
        cgv_seats_entry=Widget(),
        cgv_selector_button=Widget(),
        engine_mode_frame=Widget(),
    )

    ReservationForm._update_widgets_state(form)

    assert form.date_entry.config.get("state") == "disabled"
    assert form.time_entry.config.get("state") == "disabled"
    assert form.people_entry.config.get("state") == "disabled"
    assert form.branch_dropdown.config.get("state") == "disabled"
    assert form.theme_dropdown.config.get("state") == "disabled"
    assert form.cgv_selector_button.config.get("state") == "normal"


def test_cgv_selection_mirrors_authoritatively_into_reservation_data():
    selected_date = (date.today() + timedelta(days=7)).isoformat()
    form = SimpleNamespace(
        engine_mode_btn=Value(STANDARD_MODE),
        _site_uses_cgv=lambda: True,
        _keyescape_ui_active=lambda: False,
        yescaptcha_enabled_var=Value(False),
        yescaptcha_test_mode_var=Value(False),
        yescaptcha_client_key_entry=Widget(""),
        yescaptcha_soft_id_entry=Widget(""),
        developer_mode_enabled=lambda: False,
        config={"branches": {"CGV 용산아이파크몰": "0013"}},
        current_site="CGV",
        custom_sites=(),
        branch_var=Value("CGV 용산아이파크몰"),
        theme_var=Value("오디세이"),
        custom_theme_checkbox=Value(0),
        theme_pk_entry=Widget(""),
        date_entry=Widget("2026-08-20"),  # stale main form value
        time_entry=Widget("10:00"),       # stale main form value
        people_entry=Widget("1"),         # stale main form value
        name_entry=Widget(""),
        phone_entry=Widget(""),
        cgv_booking_mode_var=Value("회원"),
        cgv_nonmember_birth_entry=Widget(""),
        cgv_nonmember_phone_entry=Widget(""),
        cgv_nonmember_password_entry=Widget(""),
        _selected_cgv_site_no=lambda: "0013",
        cgv_selection={
            "site_no": "0013",
            "site_name": "CGV 용산아이파크몰",
            "movie": "오디세이",
            "auditorium": "IMAX관",
            "format": "IMAX LASER 2D",
            "date": selected_date,
            "people": 2,
            "show_time": "17:30",
            "preferred_times": ["17:30", "14:00"],
            "seats": "H22,H23",
            "mov_no": " 30001323 ",
            "reference_date": "2026-08-19",
        },
    )

    request, error, threads, is_sync = ReservationForm.get_reservation_data(form)

    assert error is None
    # Authoritative values originate strictly from cgv_selection
    assert request.reservation_date == selected_date
    assert request.people == 2
    assert request.reservation_time == "17:30:00"
    assert request.engine_metadata["cgv"]["preferred_times"] == ["17:30", "14:00"]
    assert request.engine_metadata["cgv"]["mov_no"] == "30001323"
    assert request.engine_metadata["cgv"]["reference_date"] == "2026-08-19"


def test_cgv_dialog_invalidation_rules_for_theater_date_movie_auditorium():
    from ui.cgv_booking_dialog import CgvBookingDialog
    from engines.cgv_client import CgvSite

    dialog = SimpleNamespace(
        selected_site=CgvSite("0013", "용산아이파크몰", "01"),
        selected_schedule={"scnsrtTm": "1400"},
        schedules=({"movNm": "오디세이", "expoScnsNm": "IMAX관"},),
        preferred_times=["14:00", "17:30"],
        priority_groups=[("H22", "H23")],
        current_seats={"H22", "H23"},
        seats=(),
        seat_recommendations={"H22": None},
        reservation_date="2026-08-26",
        date_entry=Widget("2026-08-26"),
        _request_generation=0,
        _is_restoring_initial=False,
        initial={},
        movie_var=Value("오디세이"),
        movie_menu=Widget(),
        auditorium_var=Value("IMAX관 · IMAX LASER 2D"),
        auditorium_menu=Widget(),
        target_type_badge=Widget(),
        status_label=Widget(),
        auto_seat_var=Value("명당 자동 선택"),
        auto_seat_menu=Widget(),
        load_seats_button=Widget(),
        confirm_button=Widget(),
        site_search=Widget(""),
        site_list=Widget(),
        schedule_list=Widget(),
        sites=(CgvSite("0013", "용산아이파크몰", "01"), CgvSite("0074", "왕십리", "01")),
        selected_region="",
        _start_task=lambda *args: None,
        _render_sites=lambda: None,
        _render_schedules=lambda: None,
        _render_seat_placeholder=lambda _msg: None,
        _update_seat_guide=lambda: None,
        _update_confirm_state=lambda: CgvBookingDialog._update_confirm_state(dialog),
    )

    dialog._movie_changed = lambda val="", **kw: CgvBookingDialog._movie_changed(dialog, val, **kw)
    dialog._auditorium_changed = lambda val="", **kw: CgvBookingDialog._auditorium_changed(dialog, val, **kw)
    dialog._auditorium_option = CgvBookingDialog._auditorium_option
    dialog._schedule_loaded = lambda res, **kw: CgvBookingDialog._schedule_loaded(dialog, res, **kw)
    dialog._select_site = lambda site, **kw: CgvBookingDialog._select_site(dialog, site, **kw)

    # 1. User changes Auditorium -> preferred_times, priority_groups, selected_schedule cleared
    dialog._auditorium_changed("IMAX관 · IMAX 3D", user_initiated=True)
    assert dialog.preferred_times == []
    assert dialog.priority_groups == []
    assert dialog.selected_schedule is None
    assert dialog.confirm_button.config.get("state") == "disabled"

    # Set state back
    dialog.preferred_times = ["14:00"]
    dialog.priority_groups = [("H22", "H23")]
    dialog.selected_schedule = {"scnsrtTm": "1400"}
    CgvBookingDialog._update_confirm_state(dialog)
    assert dialog.confirm_button.config.get("state") == "normal"

    # 2. User changes Movie -> preferred_times, priority_groups, selected_schedule cleared
    dialog.movie_var.set("다른 영화")
    dialog._movie_changed("다른 영화", user_initiated=True)
    assert dialog.preferred_times == []
    assert dialog.priority_groups == []
    assert dialog.selected_schedule is None
    assert dialog.confirm_button.config.get("state") == "disabled"

    # Set state back
    dialog.movie_var.set("오디세이")
    dialog.auditorium_var.set("IMAX관 · IMAX LASER 2D")
    dialog.preferred_times = ["14:00"]
    dialog.priority_groups = [("H22", "H23")]
    dialog.selected_schedule = {"scnsrtTm": "1400"}
    CgvBookingDialog._update_confirm_state(dialog)
    assert dialog.confirm_button.config.get("state") == "normal"

    # 3. User changes Date -> preferred_times, priority_groups, selected_schedule cleared
    CgvBookingDialog._change_date(dialog, "2026-08-27")
    assert dialog.preferred_times == []
    assert dialog.priority_groups == []
    assert dialog.selected_schedule is None
    assert dialog.confirm_button.config.get("state") == "disabled"

    # Set state back
    dialog.movie_var.set("오디세이")
    dialog.auditorium_var.set("IMAX관 · IMAX LASER 2D")
    dialog.preferred_times = ["14:00"]
    dialog.priority_groups = [("H22", "H23")]
    dialog.selected_schedule = {"scnsrtTm": "1400"}
    CgvBookingDialog._update_confirm_state(dialog)
    assert dialog.confirm_button.config.get("state") == "normal"

    # 4. User changes Site -> preferred_times, priority_groups, selected_schedule cleared
    new_site = CgvSite("0074", "왕십리", "01")
    dialog._select_site(new_site, user_initiated=True)
    assert dialog.preferred_times == []
    assert dialog.priority_groups == []
    assert dialog.selected_schedule is None
    assert dialog.confirm_button.config.get("state") == "disabled"


def test_cgv_dialog_initial_restoration_preserves_saved_selection_and_user_change_clears():
    from ui.cgv_booking_dialog import CgvBookingDialog
    from engines.cgv_client import CgvSite

    initial_config = {
        "site_no": "0013",
        "movie": "오디세이",
        "auditorium": "IMAX관",
        "preferred_times": ["14:00", "17:30"],
        "seats": "H22,H23",
    }

    dialog = SimpleNamespace(
        initial=initial_config,
        selected_site=None,
        selected_schedule=None,
        schedules=(),
        preferred_times=list(initial_config["preferred_times"]),
        priority_groups=[("H22", "H23")],
        current_seats=set(),
        seats=(),
        seat_recommendations={},
        reservation_date="2026-08-26",
        _request_generation=0,
        _is_restoring_initial=True,
        movie_var=Value("영화를 먼저 불러오세요"),
        movie_menu=Widget(),
        auditorium_var=Value("상영관을 먼저 불러오세요"),
        auditorium_menu=Widget(),
        target_type_badge=Widget(),
        status_label=Widget(),
        auto_seat_var=Value("명당 자동 선택"),
        auto_seat_menu=Widget(),
        load_seats_button=Widget(),
        confirm_button=Widget(),
        site_search=Widget(""),
        site_list=Widget(),
        schedule_list=Widget(),
        sites=(CgvSite("0013", "용산아이파크몰", "01"),),
        selected_region="",
        _start_task=lambda *args: None,
        _render_sites=lambda: None,
        _render_schedules=lambda: None,
        _render_seat_placeholder=lambda _msg: None,
        _update_seat_guide=lambda: None,
        _update_confirm_state=lambda: CgvBookingDialog._update_confirm_state(dialog),
    )

    dialog._movie_changed = lambda val="", **kw: CgvBookingDialog._movie_changed(dialog, val, **kw)
    dialog._auditorium_changed = lambda val="", **kw: CgvBookingDialog._auditorium_changed(dialog, val, **kw)
    dialog._auditorium_option = CgvBookingDialog._auditorium_option
    dialog._schedule_loaded = lambda res, **kw: CgvBookingDialog._schedule_loaded(dialog, res, **kw)
    dialog._select_site = lambda site, **kw: CgvBookingDialog._select_site(dialog, site, **kw)

    # Initial site selection (programmatic restore, user_initiated=False)
    dialog._select_site(dialog.sites[0], user_initiated=False)
    assert dialog.preferred_times == ["14:00", "17:30"]
    assert dialog.priority_groups == [("H22", "H23")]

    # Schedules loaded containing the initial movie and auditorium
    schedules_data = (
        {
            "expoProdNm": "오디세이",
            "expoScnsNm": "IMAX관",
            "movkndDsplEnm": "IMAX LASER 2D",
            "scnsrtTm": "1400",
            "frSeatCnt": 50,
        },
        {
            "expoProdNm": "오디세이",
            "expoScnsNm": "IMAX관",
            "movkndDsplEnm": "IMAX LASER 2D",
            "scnsrtTm": "1730",
            "frSeatCnt": 50,
        },
    )
    dialog._schedule_loaded((schedules_data, "2026-08-26", False))

    # Saved initial selection restored without losing times or seats
    assert dialog.movie_var.get() == "오디세이"
    assert "IMAX관" in dialog.auditorium_var.get()
    assert dialog.preferred_times == ["14:00", "17:30"]
    assert dialog.priority_groups == [("H22", "H23")]
    assert dialog.confirm_button.config.get("state") == "normal"
    assert dialog._is_restoring_initial is False

    # Now user manually changes movie -> stale times and seats MUST be cleared
    dialog.movie_var.set("새로운 영화")
    dialog._movie_changed("새로운 영화", user_initiated=True)
    assert dialog.preferred_times == []
    assert dialog.priority_groups == []
    assert dialog.confirm_button.config.get("state") == "disabled"


def test_cgv_dialog_restores_saved_format_exactly_when_multiple_formats_exist():
    from ui.cgv_booking_dialog import CgvBookingDialog
    from engines.cgv_client import CgvSite

    initial_config = {
        "site_no": "0013",
        "movie": "오디세이",
        "auditorium": "IMAX관",
        "format": "IMAX LASER 2D",
        "preferred_times": ["14:00", "17:30"],
        "seats": "H22,H23",
    }

    dialog = SimpleNamespace(
        initial=initial_config,
        selected_site=None,
        selected_schedule=None,
        schedules=(),
        preferred_times=list(initial_config["preferred_times"]),
        priority_groups=[("H22", "H23")],
        current_seats=set(),
        seats=(),
        seat_recommendations={},
        reservation_date="2026-08-26",
        _request_generation=0,
        _is_restoring_initial=True,
        movie_var=Value("영화를 먼저 불러오세요"),
        movie_menu=Widget(),
        auditorium_var=Value("상영관을 먼저 불러오세요"),
        auditorium_menu=Widget(),
        target_type_badge=Widget(),
        status_label=Widget(),
        auto_seat_var=Value("명당 자동 선택"),
        auto_seat_menu=Widget(),
        load_seats_button=Widget(),
        confirm_button=Widget(),
        site_search=Widget(""),
        site_list=Widget(),
        schedule_list=Widget(),
        sites=(CgvSite("0013", "용산아이파크몰", "01"),),
        selected_region="",
        _start_task=lambda *args: None,
        _render_sites=lambda: None,
        _render_schedules=lambda: None,
        _render_seat_placeholder=lambda _msg: None,
        _update_seat_guide=lambda: None,
        _update_confirm_state=lambda: CgvBookingDialog._update_confirm_state(dialog),
    )

    dialog._movie_changed = lambda val="", **kw: CgvBookingDialog._movie_changed(dialog, val, **kw)
    dialog._auditorium_changed = lambda val="", **kw: CgvBookingDialog._auditorium_changed(dialog, val, **kw)
    dialog._auditorium_option = CgvBookingDialog._auditorium_option
    dialog._schedule_loaded = lambda res, **kw: CgvBookingDialog._schedule_loaded(dialog, res, **kw)
    dialog._select_site = lambda site, **kw: CgvBookingDialog._select_site(dialog, site, **kw)

    dialog._select_site(dialog.sites[0], user_initiated=False)

    # Both IMAX 3D and IMAX LASER 2D are available
    # Options will be sorted alphabetically: ['IMAX관 · IMAX 3D', 'IMAX관 · IMAX LASER 2D']
    schedules_data = (
        {
            "expoProdNm": "오디세이",
            "expoScnsNm": "IMAX관",
            "movkndDsplEnm": "IMAX 3D",
            "scnsrtTm": "1400",
            "frSeatCnt": 50,
        },
        {
            "expoProdNm": "오디세이",
            "expoScnsNm": "IMAX관",
            "movkndDsplEnm": "IMAX LASER 2D",
            "scnsrtTm": "1400",
            "frSeatCnt": 50,
        },
        {
            "expoProdNm": "오디세이",
            "expoScnsNm": "IMAX관",
            "movkndDsplEnm": "IMAX LASER 2D",
            "scnsrtTm": "1730",
            "frSeatCnt": 50,
        },
    )
    dialog._schedule_loaded((schedules_data, "2026-08-26", False))

    # Exact format match must be selected (IMAX LASER 2D, NOT IMAX 3D)
    assert dialog.auditorium_var.get() == "IMAX관 · IMAX LASER 2D"
    assert dialog.preferred_times == ["14:00", "17:30"]
    assert dialog.priority_groups == [("H22", "H23")]
    assert dialog.confirm_button.config.get("state") == "normal"


def test_cgv_dialog_legacy_restoration_without_format_uses_auditorium_fallback():
    from ui.cgv_booking_dialog import CgvBookingDialog
    from engines.cgv_client import CgvSite

    initial_config = {
        "site_no": "0013",
        "movie": "오디세이",
        "auditorium": "IMAX관",
        "format": "",  # legacy format empty
        "preferred_times": ["14:00"],
        "seats": "H22,H23",
    }

    dialog = SimpleNamespace(
        initial=initial_config,
        selected_site=None,
        selected_schedule=None,
        schedules=(),
        preferred_times=list(initial_config["preferred_times"]),
        priority_groups=[("H22", "H23")],
        current_seats=set(),
        seats=(),
        seat_recommendations={},
        reservation_date="2026-08-26",
        _request_generation=0,
        _is_restoring_initial=True,
        movie_var=Value("영화를 먼저 불러오세요"),
        movie_menu=Widget(),
        auditorium_var=Value("상영관을 먼저 불러오세요"),
        auditorium_menu=Widget(),
        target_type_badge=Widget(),
        status_label=Widget(),
        auto_seat_var=Value("명당 자동 선택"),
        auto_seat_menu=Widget(),
        load_seats_button=Widget(),
        confirm_button=Widget(),
        site_search=Widget(""),
        site_list=Widget(),
        schedule_list=Widget(),
        sites=(CgvSite("0013", "용산아이파크몰", "01"),),
        selected_region="",
        _start_task=lambda *args: None,
        _render_sites=lambda: None,
        _render_schedules=lambda: None,
        _render_seat_placeholder=lambda _msg: None,
        _update_seat_guide=lambda: None,
        _update_confirm_state=lambda: CgvBookingDialog._update_confirm_state(dialog),
    )

    dialog._movie_changed = lambda val="", **kw: CgvBookingDialog._movie_changed(dialog, val, **kw)
    dialog._auditorium_changed = lambda val="", **kw: CgvBookingDialog._auditorium_changed(dialog, val, **kw)
    dialog._auditorium_option = CgvBookingDialog._auditorium_option
    dialog._schedule_loaded = lambda res, **kw: CgvBookingDialog._schedule_loaded(dialog, res, **kw)
    dialog._select_site = lambda site, **kw: CgvBookingDialog._select_site(dialog, site, **kw)

    dialog._select_site(dialog.sites[0], user_initiated=False)

    schedules_data = (
        {
            "expoProdNm": "오디세이",
            "expoScnsNm": "IMAX관",
            "movkndDsplEnm": "IMAX 3D",
            "scnsrtTm": "1400",
            "frSeatCnt": 50,
        },
    )
    dialog._schedule_loaded((schedules_data, "2026-08-26", False))

    # Falls back to auditorium-only match
    assert "IMAX관" in dialog.auditorium_var.get()
    assert dialog.preferred_times == ["14:00"]
    assert dialog.priority_groups == [("H22", "H23")]
    assert dialog.confirm_button.config.get("state") == "normal"


def test_all_theme_references_in_cgv_dialog_and_ui_exist():
    import glob
    import pathlib
    import re
    import ui.theme as theme

    theme_attrs = set(dir(theme))
    for pyfile in glob.glob("ui/**/*.py", recursive=True):
        content = pathlib.Path(pyfile).read_text(encoding="utf-8")
        matches = set(re.findall(r"theme\.([A-Za-z0-9_]+)", content))
        invalid = [m for m in matches if m not in theme_attrs]
        assert not invalid, f"Found invalid theme tokens in {pyfile}: {invalid}"


def test_cgv_booking_dialog_ui_construction_smoke_test():
    import customtkinter as ctk
    from ui.cgv_booking_dialog import CgvBookingDialog

    root = ctk.CTk()
    root.withdraw()
    try:
        dialog = CgvBookingDialog(
            parent=root,
            on_select=lambda data: None,
            reservation_date="2026-08-26",
            people=2,
            initial={},
        )
        assert dialog.date_entry is not None
        assert dialog.people_label is not None
        assert dialog.movie_menu is not None
        assert dialog.auditorium_menu is not None
        assert dialog.schedule_list is not None
        assert dialog.seat_list is not None
        assert dialog.confirm_button is not None
        dialog._close_dialog()
    finally:
        root.destroy()


def test_cgv_dialog_worker_performs_zero_tk_calls():
    import queue
    import threading
    import time
    from types import SimpleNamespace
    from ui.cgv_booking_dialog import CgvBookingDialog

    main_thread = threading.current_thread()

    def assert_main_thread():
        if threading.current_thread() != main_thread:
            raise AssertionError("Tk method called from background worker thread!")

    status_updates = []
    dialog = SimpleNamespace(
        _closing=False,
        _next_task_id=0,
        _active_task_id=None,
        _active_task_done=None,
        _active_cancel_event=None,
        _pending_task=None,
        _ui_event_queue=queue.Queue(),
        _task_thread_local=threading.local(),
        _request_generation=0,
        winfo_exists=lambda: (assert_main_thread(), True)[1],
        after=lambda ms, cb: (assert_main_thread(), None)[1],
        status_label=SimpleNamespace(configure=lambda **kw: (assert_main_thread(), status_updates.append(kw))[1]),
        _handle_task_error=lambda msg: None,
    )

    dialog._launch_task = lambda s, f, d: CgvBookingDialog._launch_task(dialog, s, f, d)
    dialog._start_task = lambda s, f, d: CgvBookingDialog._start_task(dialog, s, f, d)
    dialog._browser_status = lambda msg, lvl="info": CgvBookingDialog._browser_status(dialog, msg, lvl)
    dialog._poll_task = lambda: CgvBookingDialog._poll_task(dialog)
    dialog._finish_active_task = lambda **kw: CgvBookingDialog._finish_active_task(dialog, **kw)

    results = []
    def background_job(cancel_event):
        dialog._browser_status("작업 진행 중", "info")
        return "SUCCESS_RESULT"

    dialog._start_task("작업 시작", background_job, lambda res: results.append(res))

    # Drain queue via main thread
    time.sleep(0.05)
    dialog._poll_task()

    assert results == ["SUCCESS_RESULT"]
    assert any("작업 진행 중" in str(kw.get("text", "")) for kw in status_updates)


def test_cgv_dialog_long_valid_task_not_abandoned_by_time():
    import queue
    import threading
    import time
    from types import SimpleNamespace
    from ui.cgv_booking_dialog import CgvBookingDialog

    dialog = SimpleNamespace(
        _closing=False,
        _next_task_id=0,
        _active_task_id=None,
        _active_task_done=None,
        _active_cancel_event=None,
        _pending_task=None,
        _ui_event_queue=queue.Queue(),
        _task_thread_local=threading.local(),
        _request_generation=0,
        status_label=SimpleNamespace(configure=lambda **kw: None),
        _handle_task_error=lambda msg: None,
    )

    dialog._launch_task = lambda s, f, d: CgvBookingDialog._launch_task(dialog, s, f, d)
    dialog._start_task = lambda s, f, d: CgvBookingDialog._start_task(dialog, s, f, d)
    dialog._poll_task = lambda: CgvBookingDialog._poll_task(dialog)
    dialog._finish_active_task = lambda **kw: CgvBookingDialog._finish_active_task(dialog, **kw)

    results = []
    # Simulate a task that takes time
    def slow_job(cancel_event):
        time.sleep(0.06)
        return "SLOW_SUCCESS"

    dialog._start_task("느린 작업", slow_job, lambda res: results.append(res))

    # Poll multiple times while task is still running
    dialog._poll_task()
    dialog._poll_task()
    assert dialog._active_task_id == 1
    assert results == []

    # Wait for completion
    time.sleep(0.08)
    dialog._poll_task()
    assert results == ["SLOW_SUCCESS"]
    assert dialog._active_task_id is None


def test_cgv_dialog_newest_request_wins_on_rapid_site_clicks():
    import queue
    import threading
    import time
    from types import SimpleNamespace
    from ui.cgv_booking_dialog import CgvBookingDialog

    executed_tasks = []
    dialog = SimpleNamespace(
        _closing=False,
        _next_task_id=0,
        _active_task_id=None,
        _active_task_done=None,
        _active_cancel_event=None,
        _pending_task=None,
        _ui_event_queue=queue.Queue(),
        _task_thread_local=threading.local(),
        _request_generation=0,
        status_label=SimpleNamespace(configure=lambda **kw: None),
        _handle_task_error=lambda msg: None,
    )

    dialog._launch_task = lambda s, f, d: CgvBookingDialog._launch_task(dialog, s, f, d)
    dialog._start_task = lambda s, f, d: CgvBookingDialog._start_task(dialog, s, f, d)
    dialog._poll_task = lambda: CgvBookingDialog._poll_task(dialog)
    dialog._finish_active_task = lambda **kw: CgvBookingDialog._finish_active_task(dialog, **kw)

    # Task A starts
    dialog._start_task("Task A", lambda ce: (time.sleep(0.02), "A")[1], lambda res: executed_tasks.append(res))
    assert dialog._active_task_id == 1
    assert dialog._pending_task is None
    assert dialog._active_cancel_event is not None
    assert not dialog._active_cancel_event.is_set()

    # Task B requested while A is active -> becomes pending, A's cancel event set
    dialog._start_task("Task B", lambda ce: "B", lambda res: executed_tasks.append(res))
    assert dialog._active_task_id == 1
    assert dialog._pending_task is not None
    assert dialog._pending_task[0] == "Task B"
    assert dialog._active_cancel_event.is_set()

    # Task C requested while A is active -> overwrites B as newest pending
    dialog._start_task("Task C", lambda ce: (time.sleep(0.02), "C")[1], lambda res: executed_tasks.append(res))
    assert dialog._active_task_id == 1
    assert dialog._pending_task[0] == "Task C"

    # Wait for thread A to finish
    time.sleep(0.04)
    dialog._poll_task()

    # When Task A finishes, Task C starts immediately
    assert dialog._active_task_id == 2
    assert dialog._pending_task is None

    # Wait for Task C to finish
    time.sleep(0.04)
    dialog._poll_task()

    # Only C should be delivered to callback (Task B discarded, Task A skipped callback because pending won)
    assert executed_tasks == ["C"]


def test_cgv_dialog_stale_progress_ignored():
    import queue
    import threading
    from types import SimpleNamespace
    from ui.cgv_booking_dialog import CgvBookingDialog

    status_texts = []
    dialog = SimpleNamespace(
        _closing=False,
        _next_task_id=2,
        _active_task_id=2,  # Active task is ID 2
        _active_task_done=None,
        _active_cancel_event=None,
        _pending_task=None,
        _ui_event_queue=queue.Queue(),
        _task_thread_local=threading.local(),
        _request_generation=0,
        status_label=SimpleNamespace(configure=lambda **kw: status_texts.append(kw.get("text"))),
        _handle_task_error=lambda msg: None,
    )

    dialog._poll_task = lambda: CgvBookingDialog._poll_task(dialog)
    dialog._finish_active_task = lambda **kw: CgvBookingDialog._finish_active_task(dialog, **kw)

    # Obsolete progress from Task 1
    dialog._ui_event_queue.put(("progress", 1, "[CGV] 시간표 조회 완료 · 38개", "success"))
    # Valid progress from Task 2
    dialog._ui_event_queue.put(("progress", 2, "Task 2 진행 중", "info"))

    dialog._poll_task()

    assert "[CGV] 시간표 조회 완료 · 38개" not in status_texts
    assert "Task 2 진행 중" in status_texts


def test_cgv_dialog_valid_38_schedules_reaches_schedule_loaded():
    from types import SimpleNamespace
    from ui.cgv_booking_dialog import CgvBookingDialog
    from engines.cgv_client import CgvSite

    dialog = SimpleNamespace(
        _closing=False,
        _request_generation=1,
        selected_site=CgvSite("0013", "용산아이파크몰", "01"),
        selected_schedule=None,
        schedules=(),
        preferred_times=[],
        priority_groups=[],
        current_seats=set(),
        seats=(),
        seat_recommendations={},
        reservation_date="2026-08-26",
        reference_date="2026-08-26",
        reference_only=False,
        _is_restoring_initial=False,
        movie_var=SimpleNamespace(set=lambda val: None),
        movie_menu=SimpleNamespace(configure=lambda **kw: None),
        auditorium_var=SimpleNamespace(set=lambda val: None),
        auditorium_menu=SimpleNamespace(configure=lambda **kw: None),
        target_type_badge=SimpleNamespace(configure=lambda **kw: None),
        status_label=SimpleNamespace(configure=lambda **kw: None),
        auto_seat_var=SimpleNamespace(set=lambda val: None),
        auto_seat_menu=SimpleNamespace(configure=lambda **kw: None),
        load_seats_button=SimpleNamespace(configure=lambda **kw: None),
        confirm_button=SimpleNamespace(configure=lambda **kw: None, config={}),
        schedule_list=SimpleNamespace(winfo_children=lambda: []),
        _render_schedules=lambda: None,
        _render_seat_placeholder=lambda msg: None,
        _update_seat_guide=lambda: None,
        _update_confirm_state=lambda: None,
        _movie_changed=lambda movie, **kw: None,
    )

    # 38 mock schedules
    schedules = tuple(
        {
            "expoProdNm": f"Movie-{i}",
            "expoScnsNm": "IMAX관",
            "movkndDsplEnm": "IMAX LASER 2D",
            "scnsrtTm": f"{10 + (i % 12):02d}00",
            "frSeatCnt": 50,
        }
        for i in range(38)
    )

    CgvBookingDialog._schedule_loaded(dialog, (schedules, "2026-08-26", False), generation=1)

    assert len(dialog.schedules) == 38
    assert dialog.reference_only is False


def test_cgv_dialog_close_cancels_pending_tasks_and_ignores_late_results():
    import queue
    import threading
    from types import SimpleNamespace
    from ui.cgv_booking_dialog import CgvBookingDialog

    cancel_event = threading.Event()
    destroyed = False
    grab_released = False

    dialog = SimpleNamespace(
        _closing=False,
        _request_generation=5,
        _pending_task=("Pending", lambda ce: None, lambda res: None),
        _active_task_id=10,
        _active_task_done=lambda: None,
        _active_cancel_event=cancel_event,
        _ui_event_queue=queue.Queue(),
        grab_release=lambda: None,
        destroy=lambda: None,
    )

    CgvBookingDialog._close_dialog(dialog)

    assert dialog._closing is True
    assert dialog._request_generation == 6
    assert dialog._pending_task is None
    assert cancel_event.is_set()


def test_cgv_dialog_intentional_cancellation_not_displayed_as_error():
    import queue
    import threading
    from types import SimpleNamespace
    from ui.cgv_booking_dialog import CgvBookingDialog

    errors = []
    dialog = SimpleNamespace(
        _closing=False,
        _next_task_id=1,
        _active_task_id=1,
        _active_task_done=lambda res: None,
        _active_cancel_event=threading.Event(),
        _pending_task=None,
        _ui_event_queue=queue.Queue(),
        _task_thread_local=threading.local(),
        _request_generation=0,
        status_label=SimpleNamespace(configure=lambda **kw: None),
        _handle_task_error=lambda msg: errors.append(msg),
    )

    dialog._finish_active_task = lambda **kw: CgvBookingDialog._finish_active_task(dialog, **kw)
    dialog._poll_task = lambda: CgvBookingDialog._poll_task(dialog)

    # Cancelled event arrives
    dialog._ui_event_queue.put(("cancelled", 1, None, None))
    dialog._poll_task()

    # Cancelled task must NOT invoke _handle_task_error
    assert errors == []
    assert dialog._active_task_id is None






