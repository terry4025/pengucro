from __future__ import annotations

from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from threading import Event

import engines.dpsnnn_engine as module
from engines.dpsnnn_engine import (
    DPSNNN_SUBMIT_SELECTORS,
    DpsnnnEngine,
    calculate_dpsnnn_open_datetime,
    fetch_exact_dpsnnn_slots,
    fetch_dpsnnn_slots,
    parse_dpsnnn_calendar,
    resolve_dpsnnn_branch,
)
from engines.zeroworld_catalog import ZeroWorldTimeSlot
from engines.base_engine import BaseEngine


CALENDAR_HTML = """
<div class="booking_list">
  <a href="reserve_ss?idx=12&amp;day=20260809">
    <span class="text">문장 / 22:30</span><span class="booking_badge">가</span>
  </a>
</div>
<div class="booking_list waiting closed disable">
  <a href="reserve_ss?idx=13&amp;day=20260809" onclick="return false;">
    <span class="text">문장 / 21:00</span><span class="booking_badge">완</span>
  </a>
</div>
<div class="booking_list">
  <a href="reserve_ss?idx=99&amp;day=20260809">
    <span class="text">자격 / 22:15</span><span class="booking_badge">가</span>
  </a>
</div>
"""


def test_calendar_parser_filters_theme_and_marks_closed_rows():
    slots = parse_dpsnnn_calendar(CALENDAR_HTML, "2026-08-09", "문장")

    assert [(slot.time, slot.slot_id, slot.available) for slot in slots] == [
        ("21:00", "13", False),
        ("22:30", "12", True),
    ]


def test_open_datetime_is_six_days_before_target_midnight():
    open_at = calculate_dpsnnn_open_datetime("2026-08-15")

    assert open_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-09 00:00:00"


def test_unpublished_day_uses_previous_same_weekday_as_estimate(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    target = date(2026, 9, 6)

    def fake_exact(_session, _branch, _theme, source_date, _timeout):
        if source_date == "2026-08-30":
            return [ZeroWorldTimeSlot("22:30", "12", True)]
        return []

    monkeypatch.setattr(module, "fetch_exact_dpsnnn_slots", fake_exact)
    slots = fetch_dpsnnn_slots(
        "seongsu", "문장", target.isoformat(), session=Session()
    )

    assert [(slot.time, slot.slot_id, slot.available) for slot in slots] == [
        ("22:30", "", False)
    ]
    assert slots[0].estimated is True
    assert slots[0].source_date == "2026-08-30"
    assert slots[0].estimate_basis == "same_weekday"


def test_order_payload_combines_detail_and_calendar_hidden_fields():
    class Response:
        def __init__(self, text="", payload=None, url="https://dpsnnn-s.imweb.me/reserve_ss"):
            self.text = text
            self._payload = payload
            self.url = url

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Session:
        def get(self, *_args, **_kwargs):
            return Response(
                '<form id="booking_f"><input name="backurl" value="/reserve_ss">'
                '<input name="prod_idx" value="12"></form>'
            )

        def post(self, *_args, **_kwargs):
            return Response(
                payload={
                    "output": '<input name="start_day" value="2026-08-09">'
                    '<input name="start_timestamp" value="1786201200">'
                    '<input name="end_day" value="2026-08-09">'
                    '<input name="end_timestamp" value="1786201200">'
                }
            )

    engine = DpsnnnEngine(lambda *_args: None)
    payload = engine._build_order_payload(
        Session(), resolve_dpsnnn_branch("seongsu"), "12", "2026-08-09"
    )

    assert payload["prod_idx"] == "12"
    assert payload["start_timestamp"] == "1786201200"
    assert payload["unselected_end_day"] == "false"


def test_payload_prestaging_starts_three_seconds_before_open():
    open_at = calculate_dpsnnn_open_datetime("2026-08-15")

    assert not DpsnnnEngine._payload_prestage_due(
        "2026-08-15", now=open_at - timedelta(seconds=3.001)
    )
    assert DpsnnnEngine._payload_prestage_due(
        "2026-08-15", now=open_at - timedelta(seconds=3)
    )
    assert DpsnnnEngine._payload_prestage_due(
        "2026-08-15", now=open_at + timedelta(seconds=15)
    )
    assert not DpsnnnEngine._payload_prestage_due(
        "2026-08-15", now=open_at + timedelta(seconds=15.001)
    )


def test_prepared_payload_requires_matching_slot_valid_timestamps_and_fresh_age():
    payload = {
        "prod_idx": "12",
        "start_day": "2026-08-09",
        "start_timestamp": "1786201200",
        "end_day": "2026-08-09",
        "end_timestamp": "1786201200",
    }

    assert DpsnnnEngine._prepared_payload_usable(
        payload,
        "12",
        100.0,
        date_str="2026-08-09",
        now_monotonic=102.0,
    )
    assert not DpsnnnEngine._prepared_payload_usable(
        payload, "99", 100.0, now_monotonic=102.0
    )
    assert not DpsnnnEngine._prepared_payload_usable(
        payload, "12", 100.0, now_monotonic=116.0
    )
    payload["start_timestamp"] = "invalid"
    assert not DpsnnnEngine._prepared_payload_usable(
        payload, "12", 100.0, now_monotonic=102.0
    )

    payload["start_timestamp"] = "1786201200"
    payload["start_day"] = "2026-08-10"
    assert not DpsnnnEngine._prepared_payload_usable(
        payload,
        "12",
        100.0,
        date_str="2026-08-09",
        now_monotonic=102.0,
    )


def test_closed_slot_payload_is_built_before_open_and_reused(monkeypatch):
    fetch_count = 0
    build_calls = []
    add_calls = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    class Session:
        cookies = []

        def get(self, *_args, **_kwargs):
            return Response()

        def close(self):
            return None

    def fake_fetch(*_args, **_kwargs):
        nonlocal fetch_count
        fetch_count += 1
        return [ZeroWorldTimeSlot("22:30", "12", fetch_count >= 2)]

    payload = {
        "prod_idx": "12",
        "start_day": "2026-08-09",
        "start_timestamp": "1786201200",
        "end_day": "2026-08-09",
        "end_timestamp": "1786201200",
    }

    def fake_build(*_args, **_kwargs):
        build_calls.append(fetch_count)
        return dict(payload)

    def fake_add(_session, _branch, submitted, *_args, **_kwargs):
        add_calls.append((fetch_count, submitted))
        return "ORDER-1", "SUCCESS"

    monkeypatch.setattr(module, "create_dpsnnn_session", Session)
    monkeypatch.setattr(module, "fetch_exact_dpsnnn_slots", fake_fetch)
    engine = DpsnnnEngine(lambda *_args: None)
    monkeypatch.setattr(engine, "_payload_prestage_due", lambda *_args: True)
    monkeypatch.setattr(engine, "_build_order_payload", fake_build)
    monkeypatch.setattr(engine, "_add_order", fake_add)
    monkeypatch.setattr(engine, "_complete_checkout", lambda *_args: (True, "ORDER-1"))

    engine.make_reservation_thread(
        {
            "branch": "seongsu",
            "themePK": "문장",
            "reservationDate": "2026-08-09",
            "reservationTime": "22:30:00",
        }
    )

    assert build_calls == [1]
    assert add_calls == [(2, payload)]


def test_failed_order_discards_prestaged_payload_before_retry(monkeypatch):
    fetch_count = 0
    build_calls = []
    add_calls = 0

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    class Session:
        cookies = []

        def get(self, *_args, **_kwargs):
            return Response()

        def close(self):
            return None

    def fake_fetch(*_args, **_kwargs):
        nonlocal fetch_count
        fetch_count += 1
        return [ZeroWorldTimeSlot("22:30", "12", fetch_count >= 2)]

    payload = {
        "prod_idx": "12",
        "start_day": "2026-08-09",
        "start_timestamp": "1786201200",
        "end_day": "2026-08-09",
        "end_timestamp": "1786201200",
    }

    def fake_build(*_args, **_kwargs):
        build_calls.append(fetch_count)
        return dict(payload)

    def fake_add(*_args, **_kwargs):
        nonlocal add_calls
        add_calls += 1
        if add_calls == 1:
            return "", "SOLD_OUT"
        return "ORDER-2", "SUCCESS"

    monkeypatch.setattr(module, "create_dpsnnn_session", Session)
    monkeypatch.setattr(module, "fetch_exact_dpsnnn_slots", fake_fetch)
    engine = DpsnnnEngine(lambda *_args: None)
    monkeypatch.setattr(engine, "_payload_prestage_due", lambda *_args: True)
    monkeypatch.setattr(engine, "_build_order_payload", fake_build)
    monkeypatch.setattr(engine, "_add_order", fake_add)
    monkeypatch.setattr(engine, "_complete_checkout", lambda *_args: (True, "ORDER-2"))

    engine.make_reservation_thread(
        {
            "branch": "seongsu",
            "themePK": "문장",
            "reservationDate": "2026-08-09",
            "reservationTime": "22:30:00",
        }
    )

    assert build_calls == [1, 3]
    assert add_calls == 2


def test_engine_clamps_parallel_workers_to_measured_limit(monkeypatch):
    captured = {}
    logs = []

    def fake_start(_self, _data, workers, is_async=False):
        captured.update(workers=workers, is_async=is_async)

    monkeypatch.setattr(BaseEngine, "start_reservation", fake_start)
    engine = DpsnnnEngine(lambda message, level: logs.append((message, level)))

    engine.start_reservation(
        {
            "branch": "seongsu",
            "themePK": "문장",
            "reservationDate": "2026-08-15",
            "reservationTime": "22:30:00",
            "devMode": True,
        },
        50,
        is_async=True,
    )

    assert captured == {"workers": module.DPSNNN_MAX_WORKERS, "is_async": False}
    assert any("2026-08-09 00:00" in message for message, _level in logs)


def test_parallel_watchers_create_exactly_one_order(monkeypatch):
    workers = 4
    barrier = Barrier(workers)
    calls = 0
    calls_lock = Lock()

    class Response:
        def raise_for_status(self):
            return None

    class Session:
        cookies = []

        def get(self, *_args, **_kwargs):
            return Response()

        def close(self):
            return None

    def fake_fetch(*_args, **_kwargs):
        barrier.wait(timeout=3)
        return [ZeroWorldTimeSlot("22:30", "12", True)]

    def fake_add_order(*_args, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        return "ORDER-1", "SUCCESS"

    monkeypatch.setattr(module, "create_dpsnnn_session", Session)
    monkeypatch.setattr(module, "fetch_exact_dpsnnn_slots", fake_fetch)
    engine = DpsnnnEngine(lambda *_args: None)
    monkeypatch.setattr(engine, "_build_order_payload", lambda *_args: {})
    monkeypatch.setattr(engine, "_add_order", fake_add_order)
    monkeypatch.setattr(engine, "_complete_checkout", lambda *_args: (True, "ORDER-1"))
    payload = {
        "branch": "seongsu",
        "themePK": "문장",
        "reservationDate": "2026-08-09",
        "reservationTime": "22:30:00",
    }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(engine.make_reservation_thread, payload) for _ in range(workers)]
        for future in futures:
            future.result(timeout=5)

    assert calls == 1
    assert engine._order_claimed.is_set()


def test_exact_slot_fetch_reports_safe_http_metadata():
    diagnostics = []

    class Response:
        status_code = 200
        text = CALENDAR_HTML

        def raise_for_status(self):
            return None

    class Session:
        def post(self, *_args, **_kwargs):
            return Response()

    slots = fetch_exact_dpsnnn_slots(
        Session(),
        resolve_dpsnnn_branch("seongsu"),
        "문장",
        "2026-08-09",
        diagnostics=lambda *items: diagnostics.append(items),
    )

    assert len(slots) == 2
    assert diagnostics
    stage, method, status, elapsed, detail = diagnostics[0]
    assert (stage, method, status, detail) == ("시간표 조회", "POST", 200, "")
    assert elapsed >= 0


def test_dpsnnn_exception_description_is_useful_when_message_is_blank():
    assert DpsnnnEngine._describe_exception(TimeoutError()) == "TimeoutError"


def test_dpsnnn_http_log_identifies_worker_stage_status_and_rtt():
    logs = []
    engine = DpsnnnEngine(
        lambda message, level: logs.append((message, level))
    )

    engine._log_http_diagnostic(
        "작업 4", "예약 주문 생성", "POST", 429, 0.087, force=True
    )

    message, level = logs[-1]
    assert "[작업 4]" in message
    assert "예약 주문 생성" in message
    assert "status=429" in message
    assert "RTT 87ms" in message
    assert level == "warning"


def test_imweb_checkout_targets_real_anchor_submit_control():
    assert "form#order_payment a._btn_start_payment" in DPSNNN_SUBMIT_SELECTORS


def test_checkout_success_requires_confirmation_not_payment_form():
    payment_url = "https://www.dpsnnn.com/shop_payment/?order_code=ORDER-1"

    assert not DpsnnnEngine._checkout_success(
        "전체 동의 개인정보 수집 및 이용 동의 결제하기",
        payment_url,
        payment_url,
    )
    assert DpsnnnEngine._checkout_success(
        "주문이 정상적으로 접수되었습니다. 주문 번호 ORDER-1",
        "https://www.dpsnnn.com/shop_payment_complete/",
        payment_url,
    )
    assert not DpsnnnEngine._checkout_success(
        "로그인이 필요합니다.",
        "https://www.dpsnnn.com/login",
        payment_url,
    )
    assert DpsnnnEngine._checkout_number(
        "주문 번호: ORDER-123", "ORDER-1"
    ) == "ORDER-123"
    assert DpsnnnEngine._checkout_number(
        "주문 번호: o2026081403e2e46c49bbc",
        "o2026081403e2e46c49bbc",
    ) == ""
    assert DpsnnnEngine._checkout_number(
        "",
        "o2026081403e2e46c49bbc",
        "https://www.dpsnnn.com/shop_payment_complete/?order_no=O20260814-00001",
    ) == "O20260814-00001"


def test_imweb_styled_control_uses_label_instead_of_input_check():
    events = []

    class Label:
        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self, force=False):
            events.append(("label-click", force))
            control.checked = True

    class Control:
        checked = False

        def wait_for(self, **_kwargs):
            events.append("wait")

        def is_checked(self):
            return self.checked

        def locator(self, selector):
            assert selector == "xpath=ancestor::label[1]"
            return Label()

        def evaluate(self, _script):
            events.append("fallback")

    class Page:
        def wait_for_timeout(self, _milliseconds):
            events.append("poll")

    control = Control()

    assert DpsnnnEngine._activate_labeled_control(Page(), control)
    assert ("label-click", True) in events
    assert "fallback" not in events


def test_imweb_orderer_fields_wait_for_dynamic_render_and_validate_values():
    events = []

    class Field:
        def __init__(self, selector):
            self.selector = selector
            self.value = ""

        def wait_for(self, **kwargs):
            events.append(("field-wait", self.selector, kwargs))

        def is_visible(self):
            return True

        def fill(self, value):
            self.value = value
            events.append(("fill", self.selector, value))

        def evaluate(self, _script):
            events.append(("change", self.selector))

        def input_value(self):
            return self.value

    class Locator:
        def __init__(self, field):
            self.first = field

    class Page:
        def __init__(self):
            self.fields = {
                module.DPSNNN_ORDERER_NAME_SELECTOR: Field(
                    module.DPSNNN_ORDERER_NAME_SELECTOR
                ),
                module.DPSNNN_ORDERER_CALL_SELECTOR: Field(
                    module.DPSNNN_ORDERER_CALL_SELECTOR
                ),
            }

        def wait_for_selector(self, selector, **kwargs):
            events.append(("page-wait", selector, kwargs))

        def locator(self, selector):
            return Locator(self.fields[selector])

        def wait_for_function(self, _script, *, arg, timeout):
            events.append(("validate", arg, timeout))
            assert self.fields[arg["nameSelector"]].value == arg["name"]
            actual_digits = "".join(
                character
                for character in self.fields[arg["phoneSelector"]].value
                if character.isdigit()
            )
            assert actual_digits == arg["phoneDigits"]

    page = Page()
    prepared, error = DpsnnnEngine._prepare_orderer_checkout(
        page,
        "  홍길동  ",
        "010-1234-5678",
    )

    assert prepared and error == ""
    assert events[0][0:2] == ("page-wait", module.DPSNNN_PAYMENT_READY_SELECTOR)
    assert (
        "fill",
        module.DPSNNN_ORDERER_NAME_SELECTOR,
        "홍길동",
    ) in events
    assert (
        "fill",
        module.DPSNNN_ORDERER_CALL_SELECTOR,
        "010-1234-5678",
    ) in events
    assert any(event[0] == "validate" for event in events)


def test_imweb_orderer_fields_reject_invalid_contact_before_payment():
    class Page:
        def wait_for_selector(self, *_args, **_kwargs):
            raise AssertionError("invalid data must fail before touching the page")

    assert DpsnnnEngine._prepare_orderer_checkout(Page(), "김", "010-1234-5678") == (
        False,
        "예약자 이름이 올바르지 않습니다.",
    )
    assert DpsnnnEngine._prepare_orderer_checkout(Page(), "김철수", "123") == (
        False,
        "예약자 연락처가 올바르지 않습니다.",
    )


def test_retained_checkout_browser_releases_slot_after_user_closes(monkeypatch):
    released = Event()

    class Chrome:
        port = 9333

        def release(self):
            released.set()

    monkeypatch.setattr(module.browser_session, "cdp_descriptor", lambda _port: None)

    DpsnnnEngine._release_browser_lease_when_closed(Chrome())

    assert released.wait(timeout=2)
