import base64
import io
from threading import Event

from PIL import Image
import pytest

from engines import cgv_login as module


def png():
    output = io.BytesIO()
    Image.new('RGB', (240, 95), 'white').save(output, 'PNG')
    return output.getvalue()


class Page:
    url = 'https://cgv.co.kr/mem/login?nmbrAtktFlag=Y'

    def __init__(self):
        self.state = {'image': 'data:image/png;base64,' + base64.b64encode(png()).decode(),
                      'credentialsReady': True, 'editingCredentials': False, 'captchaFilled': False}
        self.fills = []
        self.submissions = 0
        self.fail_after_submit = False

    def evaluate(self, script, arg=None):
        if script == module._READ_FORM:
            return dict(self.state)
        assert script == module._SUBMIT_UNCHANGED
        assert arg['answer'] == '000701'
        self.submissions += 1
        if self.fail_after_submit:
            raise RuntimeError('do not expose sensitive browser values')
        return True

    def locator(self, selector):
        assert selector == '#loginInput3'
        return self

    def fill(self, answer, timeout):
        self.fills.append(answer)
        self.state['captchaFilled'] = True


@pytest.fixture
def login(monkeypatch):
    monkeypatch.setattr(module, 'recognize_cgv_digits', lambda _raw: '000701')
    logs = []
    assistant = module.CgvLoginAssistant(lambda text, level: logs.append((text, level)), Event())
    return assistant, logs


def test_login_submits_once_without_logging_answer_or_claiming_success(login):
    assistant, logs = login
    page = Page()
    for _ in range(4):
        assistant.step(page)
    assert page.fills == ['000701']
    assert page.submissions == 1
    assert all(level != 'success' and '000701' not in text for text, level in logs)


@pytest.mark.parametrize('field,value', [('credentialsReady', False), ('editingCredentials', True),
                                        ('captchaFilled', True)])
def test_missing_credentials_active_editing_and_manual_captcha_are_untouched(login, field, value):
    assistant, _ = login
    page = Page()
    page.state[field] = value
    assistant.step(page)
    assert not page.fills and not page.submissions


@pytest.mark.parametrize('url', ['https://cgv.co.kr.attacker.test/mem/login',
                                 'http://cgv.co.kr/mem/login', 'https://cgv.co.kr/cnm/payment'])
def test_only_official_https_member_login_form_is_supported(login, url):
    assistant, _ = login
    page = Page()
    page.url = url
    assistant.step(page)
    assert not page.fills and not page.submissions


def test_stop_before_or_during_recognition_prevents_fill_and_submit(login, monkeypatch):
    assistant, _ = login
    page = Page()
    assistant.stop_event.set()
    assistant.step(page)
    assistant.stop_event.clear()

    def stop(_raw):
        assistant.stop_event.set()
        return '000701'

    monkeypatch.setattr(module, 'recognize_cgv_digits', stop)
    assistant.step(page)
    assert not page.fills and not page.submissions


def test_changed_challenge_is_not_filled_with_stale_answer(login, monkeypatch):
    assistant, _ = login
    page = Page()

    def changed(_raw):
        page.state['image'] += 'changed'
        return '000701'

    monkeypatch.setattr(module, 'recognize_cgv_digits', changed)
    assistant.step(page)
    assert not page.fills and not page.submissions


def test_ambiguous_login_click_is_never_retried(login):
    assistant, logs = login
    page = Page()
    page.fail_after_submit = True
    assistant.step(page)
    page.state['captchaFilled'] = False
    assistant.step(page)
    assert page.submissions == 1
    assert all('sensitive' not in text for text, _ in logs)


def test_ocr_failure_is_not_recomputed_in_every_wait_tick(login, monkeypatch):
    assistant, _ = login
    page = Page()
    calls = []
    monkeypatch.setattr(module, 'recognize_cgv_digits', lambda raw: calls.append(raw) or '')
    for _ in range(5):
        assistant.step(page)
    assert len(calls) == 1
    assert not page.fills and not page.submissions


def test_stop_after_fill_prevents_login_submission(login, monkeypatch):
    assistant, _ = login
    page = Page()
    original = page.fill

    def fill(answer, timeout):
        original(answer, timeout)
        assistant.stop_event.set()

    monkeypatch.setattr(page, 'fill', fill)
    assistant.step(page)
    assert page.fills and not page.submissions


@pytest.mark.parametrize('candidates,expected', [
    (['000701', '000701'], '000701'),
    (['000701', '000702', '000701', '000701'], '000701'),
    (['000701', '000702', '000701', '000702'], ''),
    (['00070', '00070', '00070', '00070'], ''),
    (['abc123', 'abc123', 'abc123', 'abc123'], ''),
])
def test_cgv_requires_six_digits_and_agreeing_pixel_model_outputs(monkeypatch, candidates, expected):
    outputs = iter(candidates)

    def recognize(_raw, _beta, _width, *, expected_length):
        assert expected_length == 6
        return [next(outputs)]

    monkeypatch.setattr(module, '_recognize', recognize)
    assert module.recognize_cgv_digits(png()) == expected


def test_six_digit_decoder_extension_preserves_zeroworld_default_length():
    import numpy as np
    from engines.zeroworld_captcha import _decode_candidates

    charset = ['', *'0123456789']
    scores = np.full((12, 1, len(charset)), -30.0)
    for index, digit in enumerate('000701'):
        scores[index * 2, 0, charset.index(digit)] = 0
        scores[index * 2 + 1, 0, 0] = 0
    assert _decode_candidates(scores, charset, min_length=6, max_length=6)[0] == '000701'
    assert all(4 <= len(candidate) <= 5 for candidate in _decode_candidates(scores, charset))


@pytest.mark.parametrize('entrypoint', ['engine', 'visitor', 'seat_map'])
def test_existing_login_wait_paths_invoke_assistant_and_require_session_transition(monkeypatch, entrypoint):
    from engines import cgv_engine, cgv_engine_visitor_runtime, cgv_browser_client

    class LoginPage:
        url = 'https://cgv.co.kr/mem/login'
        logged_in = False
        calls = 0

        def __init__(self):
            self.context = self

        def cookies(self, _url):
            return [{'name': 'accessToken', 'value': 'fixture-token'}] if self.logged_in else []

        def goto(self, url, **_kwargs):
            self.url = url

        def is_closed(self):
            return False

        def wait_for_timeout(self, _milliseconds):
            assert self.calls <= 1

    class Assistant:
        def __init__(self, _log, _stop_event):
            pass

        def step(self, page):
            page.calls += 1
            page.logged_in = True
            page.url = 'https://cgv.co.kr/'

    page = LoginPage()
    target = {'engine': cgv_engine, 'visitor': cgv_engine_visitor_runtime,
              'seat_map': cgv_browser_client}[entrypoint]
    monkeypatch.setattr(target, 'CgvLoginAssistant', Assistant)
    if entrypoint == 'seat_map':
        client = target.CgvBrowserClient()
        monkeypatch.setattr(client, '_wait_for_post_login_navigation', lambda _page: None)
        client._wait_for_member_login(page, require_fresh_login=True)
    else:
        engine = target.CgvEngine(lambda *_args: None)
        if entrypoint == 'visitor':
            assert engine._wait_for_manual_login(page)
        else:
            assert engine._ensure_member_session(page, page.context)
    assert page.calls == 1
    assert page.logged_in


def test_seat_map_login_wait_honors_cancel_before_any_page_access():
    from engines.cgv_browser_client import CgvBrowserClient, CgvRequestCancelled

    stop = Event()
    stop.set()
    with pytest.raises(CgvRequestCancelled):
        CgvBrowserClient()._wait_for_member_login(object(), cancel_event=stop)
