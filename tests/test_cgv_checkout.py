import pytest

from engines.cgv_engine import CgvEngine


def _engine(logs=None, successes=None):
    logs = logs if logs is not None else []
    successes = successes if successes is not None else []
    return CgvEngine(
        lambda message, level: logs.append((message, level)),
        success_callback=lambda: successes.append(True),
    )


def test_naver_payment_url_accepts_same_tab_instant_pay_shell():
    assert CgvEngine._is_naver_payment_url(
        "https://financial.pstatic.net/orders/instantPay/build/checkout"
    )
    assert CgvEngine._is_naver_payment_url(
        "https://order.pay.naver.com/instantPay/nfPayment/123"
    )
    assert not CgvEngine._is_naver_payment_url("https://cgv.co.kr/mpy/main")


def test_naver_login_url_is_distinguished_from_payment_page():
    assert CgvEngine._is_naver_login_url(
        "https://nid.naver.com/nidlogin.login?url=https%3A%2F%2Fm.pay.naver.com%2Fz"
    )
    assert not CgvEngine._is_naver_login_url(
        "https://m.pay.naver.com/z/payments/example"
    )


def test_prefilled_naver_login_click_returns_presence_only():
    class Page:
        @staticmethod
        def evaluate(_script, allow_click):
            assert allow_click is True
            return {"found": True, "filled": True, "clicked": True}

    assert CgvEngine._click_prefilled_naver_login(Page()) == {
        "found": True,
        "filled": True,
        "clicked": True,
    }


def test_naver_additional_verification_is_detected_without_reading_credentials():
    class Page:
        @staticmethod
        def evaluate(_script):
            return True

    assert CgvEngine._naver_additional_verification_visible(Page()) is True


def test_naver_login_is_resumed_before_developer_mode_stops(monkeypatch):
    engine = _engine()
    cgv_page = object()
    login_page = object()
    payment_page = object()
    calls = []
    monkeypatch.setattr(engine, "_advance_to_cgv_payment_methods", lambda _page: True)
    monkeypatch.setattr(engine, "_select_cgv_npay_method", lambda _page: True)
    monkeypatch.setattr(engine, "_accept_cgv_payment_terms", lambda _page: True)
    monkeypatch.setattr(engine, "_open_naver_payment_page", lambda _page: login_page)
    monkeypatch.setattr(
        engine,
        "_ensure_naver_payment_session",
        lambda page: calls.append(("login", page)) or payment_page,
    )
    monkeypatch.setattr(
        engine,
        "_prepare_naver_card",
        lambda _page: calls.append(("card", _page)) or True,
    )

    assert engine._proceed_naver_pay_checkout(cgv_page, developer_mode=True) is True
    assert calls == [("login", login_page)]


def test_checkout_runs_two_cgv_stages_before_naver_final_payment(monkeypatch):
    engine = _engine()
    cgv_page = object()
    naver_page = object()
    calls = []

    monkeypatch.setattr(
        engine,
        "_advance_to_cgv_payment_methods",
        lambda page: calls.append(("first-cgv-pay", page)) or True,
    )
    monkeypatch.setattr(
        engine,
        "_select_cgv_npay_method",
        lambda page: calls.append(("select-npay", page)) or True,
    )
    monkeypatch.setattr(
        engine,
        "_accept_cgv_payment_terms",
        lambda page: calls.append(("accept-terms", page)) or True,
    )
    monkeypatch.setattr(
        engine,
        "_open_naver_payment_page",
        lambda page: calls.append(("second-cgv-pay", page)) or naver_page,
    )
    monkeypatch.setattr(
        engine,
        "_prepare_naver_card",
        lambda page: calls.append(("select-card", page)) or True,
    )
    monkeypatch.setattr(
        engine,
        "_click_naver_final_payment",
        lambda page: calls.append(("final-naver-pay", page)) or True,
    )
    monkeypatch.setattr(
        engine,
        "_wait_for_cgv_payment_confirmation",
        lambda parent, payment: calls.append(("confirm", parent, payment)) or True,
    )

    assert engine._proceed_naver_pay_checkout(cgv_page) is True
    assert [call[0] for call in calls] == [
        "first-cgv-pay",
        "select-npay",
        "accept-terms",
        "second-cgv-pay",
        "select-card",
        "final-naver-pay",
        "confirm",
    ]


def test_developer_checkout_stops_before_card_and_final_payment(monkeypatch):
    engine = _engine()
    cgv_page = object()
    naver_page = object()
    calls = []

    monkeypatch.setattr(engine, "_advance_to_cgv_payment_methods", lambda _page: True)
    monkeypatch.setattr(engine, "_select_cgv_npay_method", lambda _page: True)
    monkeypatch.setattr(engine, "_accept_cgv_payment_terms", lambda _page: True)
    monkeypatch.setattr(engine, "_open_naver_payment_page", lambda _page: naver_page)
    monkeypatch.setattr(
        engine,
        "_prepare_naver_card",
        lambda _page: calls.append("card") or True,
    )
    monkeypatch.setattr(
        engine,
        "_click_naver_final_payment",
        lambda _page: calls.append("pay") or True,
    )

    assert engine._proceed_naver_pay_checkout(cgv_page, developer_mode=True) is True
    assert calls == []


@pytest.mark.parametrize(
    ("failed_stage", "expected_calls"),
    [
        ("advance", ["advance"]),
        ("npay", ["advance", "npay"]),
        ("terms", ["advance", "npay", "terms"]),
        ("open", ["advance", "npay", "terms", "open"]),
        ("card", ["advance", "npay", "terms", "open", "card"]),
        (
            "final",
            ["advance", "npay", "terms", "open", "card", "final"],
        ),
        (
            "confirm",
            ["advance", "npay", "terms", "open", "card", "final", "confirm"],
        ),
    ],
)
def test_checkout_failure_never_becomes_success(
    monkeypatch,
    failed_stage,
    expected_calls,
):
    engine = _engine()
    cgv_page = object()
    naver_page = object()
    calls = []

    def stage(name, success=True):
        def run(*_args):
            calls.append(name)
            return success

        return run

    monkeypatch.setattr(
        engine,
        "_advance_to_cgv_payment_methods",
        stage("advance", failed_stage != "advance"),
    )
    monkeypatch.setattr(
        engine,
        "_select_cgv_npay_method",
        stage("npay", failed_stage != "npay"),
    )
    monkeypatch.setattr(
        engine,
        "_accept_cgv_payment_terms",
        stage("terms", failed_stage != "terms"),
    )

    def open_page(*_args):
        calls.append("open")
        return None if failed_stage == "open" else naver_page

    monkeypatch.setattr(engine, "_open_naver_payment_page", open_page)
    monkeypatch.setattr(
        engine,
        "_prepare_naver_card",
        stage("card", failed_stage != "card"),
    )
    monkeypatch.setattr(
        engine,
        "_click_naver_final_payment",
        stage("final", failed_stage != "final"),
    )
    monkeypatch.setattr(
        engine,
        "_wait_for_cgv_payment_confirmation",
        stage("confirm", failed_stage != "confirm"),
    )

    assert engine._proceed_naver_pay_checkout(cgv_page) is False
    assert calls == expected_calls


def test_unconfirmed_checkout_does_not_fire_success_callback():
    logs = []
    successes = []
    engine = _engine(logs, successes)

    assert not engine._report_checkout_outcome(
        checkout_completed=False,
        developer_mode=False,
        site_no="0013",
        movie="오디세이",
    )
    assert successes == []
    assert engine.stop_event.is_set() is False
    assert any("예매 성공으로 처리하지" in message for message, _level in logs)


def test_confirmed_checkout_fires_success_once():
    logs = []
    successes = []
    engine = _engine(logs, successes)

    assert engine._report_checkout_outcome(
        checkout_completed=True,
        developer_mode=False,
        site_no="0013",
        movie="오디세이",
    )
    assert successes == [True]
    assert engine.stop_event.is_set()
    assert any("예매 완료를 확인" in message for message, _level in logs)
