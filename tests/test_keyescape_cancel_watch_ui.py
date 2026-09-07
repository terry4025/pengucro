"""One-run Keyescape cancellation-watch opt-in; no external requests."""

import queue
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from pengucro.models import NAVER_MODE, STANDARD_MODE, TRIPCOM_MODE, ReservationRequest
from engines.base_engine import BaseEngine
from engines.keyescape_engine_runtime import KeyescapeEngine
from ui.main_window import MainWindow
from ui.reservation_form import ReservationForm


class Widget:
    def __init__(self, value=False):
        self.value = value
        self.config = {}

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def configure(self, **kwargs):
        self.config.update(kwargs)

    def deselect(self):
        self.value = False


class Form(SimpleNamespace):
    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        widget = Widget()
        setattr(self, name, widget)
        return widget


def make_form(*, enabled=False, mode=STANDARD_MODE, keyescape=True):
    site = '호환 키이스' if keyescape else '다른 사이트'
    config = {
        'url': 'https://example.invalid/reservation',
        'engine_id': 'keyescape' if keyescape else 'jigubyeol',
        'branches': {'본점': '1'},
        'themes': {'1': {'시험 테마': '2'}},
    }
    form = Form(
        current_site=site, custom_sites={site: config}, config=config,
        engine_mode_btn=Widget(mode), branch_var=Widget('본점'), theme_var=Widget('시험 테마'),
        day_type_var=Widget('평일'), custom_theme_checkbox=Widget(False), theme_pk_entry=Widget(''),
        date_entry=Widget((date.today() + timedelta(days=7)).isoformat()), time_entry=Widget('13:40'),
        name_entry=Widget('테스트'), phone_entry=Widget('01000000001'), people_entry=Widget('2'),
        threads_slider=Widget(3), keyescape_cancel_watch_checkbox=Widget(enabled),
        yescaptcha_enabled_var=Widget(False), yescaptcha_test_mode_var=Widget(False),
        yescaptcha_client_key_entry=Widget(''), yescaptcha_soft_id_entry=Widget(''),
        standard_threads=8, naver_threads=1, keyescape_threads=3, dpsnnn_threads=4, cgv_threads=1,
        cgv_selection={}, cgv_booking_mode_var=Widget('회원'),
        _site_uses_cgv=lambda: False, _site_uses_dpsnnn=lambda: False,
        _selected_branch_id=lambda: '1', _selected_theme_id=lambda: '2',
        developer_mode_enabled=lambda: False, _update_widgets_state=lambda: None,
    )
    form._keyescape_ui_active = lambda: keyescape and form.engine_mode_btn.get() != NAVER_MODE
    return form


@pytest.mark.parametrize('enabled', [False, True])
def test_opt_in_reaches_model_and_payload_without_changing_normal_slider(enabled):
    form = make_form(enabled=enabled)
    request, error, threads, is_async = ReservationForm.get_reservation_data(form)
    assert error is None and is_async
    assert request.keyescape_cancel_watch is enabled
    payload = request.to_engine_payload()
    if enabled:
        assert payload['keyescape_cancel_watch'] is True
        assert payload['keyescape_cancel_watch_seconds'] == 600
        assert '1페이지' in request.summary() and '최대 10분' in request.summary()
        assert '예약 가능 슬롯 확인 후 시도' in request.summary()
    else:
        assert 'keyescape_cancel_watch' not in payload
        assert 'keyescape_cancel_watch_seconds' not in payload
        assert '취소표 대기' not in request.summary()
    assert threads == (1 if enabled else 3)
    assert form.threads_slider.get() == 3


@pytest.mark.parametrize('mode,keyescape', [
    (NAVER_MODE, True), (TRIPCOM_MODE, True), (STANDARD_MODE, False),
])
def test_stale_opt_in_is_cleared_outside_keyescape_standard_mode(mode, keyescape):
    form = make_form(enabled=True, mode=mode, keyescape=keyescape)
    assert not ReservationForm.keyescape_cancel_watch_enabled(form)
    ReservationForm._update_keyescape_cancel_watch_state(form)
    assert form.keyescape_cancel_watch_checkbox.get() is False
    assert form.keyescape_cancel_watch_checkbox.config['state'] == 'disabled'


def test_other_engine_never_receives_stale_cancel_watch_options():
    form = make_form(enabled=True, keyescape=False)
    request, error, _, _ = ReservationForm.get_reservation_data(form)
    assert error is None
    assert 'keyescape_cancel_watch' not in request.to_engine_payload()


def test_start_consumes_opt_in_after_payload_capture_and_locks_widget():
    form = make_form(enabled=True)
    request, error, _, _ = ReservationForm.get_reservation_data(form)
    assert error is None
    payload = request.to_engine_payload()
    ReservationForm.set_running_state(form, True)
    assert payload['keyescape_cancel_watch'] is True
    assert form.keyescape_cancel_watch_checkbox.get() is False
    assert form.keyescape_cancel_watch_checkbox.config['state'] == 'disabled'
    ReservationForm.set_running_state(form, False)
    assert form.keyescape_cancel_watch_checkbox.config['state'] == 'normal'
    assert not ReservationForm.keyescape_cancel_watch_enabled(form)
    next_request, error, threads, _ = ReservationForm.get_reservation_data(form)
    assert error is None and threads == 3
    assert 'keyescape_cancel_watch' not in next_request.to_engine_payload()


def test_cancel_watch_is_never_in_saved_form_config():
    values = ReservationForm._current_config_values(make_form(enabled=True), '호환 키이스')
    assert 'keyescape_cancel_watch' not in values
    assert 'keyescape_cancel_watch_seconds' not in values


@pytest.mark.parametrize('value', [False, 'false', 'off', '0', 0, ''])
def test_model_does_not_enable_cancel_watch_for_false_like_values(value):
    request = ReservationRequest.from_mapping('키이스케이프', {'keyescape_cancel_watch': value})
    assert request.keyescape_cancel_watch is False
    assert 'keyescape_cancel_watch' not in request.to_engine_payload()


def test_model_uses_fixed_finite_watch_window():
    request = ReservationRequest.from_mapping('키이스케이프', {
        'keyescape_cancel_watch': True, 'keyescape_cancel_watch_seconds': 999999,
    })
    assert request.to_engine_payload()['keyescape_cancel_watch_seconds'] == 600


def test_mainwindow_registry_delivers_confirmed_opt_in_before_form_resets(monkeypatch, tmp_path):
    monkeypatch.setenv('PENGUCRO_DATA_DIR', str(tmp_path))
    form = make_form(enabled=True)
    form.save_config = Mock()
    form.set_running_state = lambda running: ReservationForm.set_running_state(form, running)
    request, error, threads, is_async = ReservationForm.get_reservation_data(form)
    assert error is None
    app = SimpleNamespace(
        _catalog_refresh_running=False, _keyescape_cache_running=False,
        site_var=Widget(form.current_site), form=form, custom_sites=form.custom_sites,
        log_panel=Mock(), site_dropdown=Mock(), add_site_btn=Mock(), delete_site_btn=Mock(),
        cta_btn=Mock(), _set_status_badge=Mock(), engine_event_queue=queue.Queue(),
        _on_engine_log=Mock(), _on_booking_success=Mock(), _on_engine_status_update=Mock(),
        _on_engine_log_batch=Mock(), _update_booking_status=Mock(), _reset_cta_state=Mock(),
    )
    # Keep the actual UI/registry/engine dispatch, but never start a browser or HTTP worker.
    with patch.object(BaseEngine, 'start_reservation', autospec=True) as start:
        MainWindow._start_booking(app, request, threads, is_async)
        start.assert_called_once()
        engine, payload = start.call_args.args
        worker_count = start.call_args.kwargs['num_threads']
        assert isinstance(engine, KeyescapeEngine)
        assert payload['keyescape_cancel_watch'] is True
        assert payload['keyescape_cancel_watch_seconds'] == 600
        assert worker_count == 1
    assert form.keyescape_cancel_watch_checkbox.get() is False
    assert form.threads_slider.get() == 3
