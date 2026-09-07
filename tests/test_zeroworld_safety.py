import asyncio
import hashlib
import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from engines import zeroworld_captcha as captcha
from engines.zeroworld_shin_engine import ZeroWorldShinEngine, ZeroWorldAuthenticationRequired
from engines.time_slot_fetchers import fetch_any_time_slots
from pengucro.models import BookingResult


def engine_context():
    engine = ZeroWorldShinEngine("https://zero.example", lambda *_: None)
    context = engine._build_context(dict(branch="5", reservationDate="2026-09-20",
        reservationTime="13:40", themePK="9", theme_name="시험 테마", name="테스트", phone="01000000000"))
    return engine, context


@pytest.mark.parametrize("text,success", [
    ("예약번호 12345 예약상태 신청 2026-09-20 13:40 시험 테마", True),
    ("예약번호 12345 예약상태 환불 2026-09-20 13:40 시험 테마", False),
    ("예약번호 12345 예약상태 취소 2026-09-20 13:40 시험 테마", False),
    ("예약번호 12345 예약상태 신청 2026-09-21 13:40 시험 테마", False),
    ("예약번호 12345 예약상태 신청 2026-09-20 14:40 시험 테마", False),
    ("예약번호 12345 예약상태 신청 2026-09-20 13:40 다른 테마", False),
    ("완료 접수 성공 ck_code=12345", False),
    ("예약상태 신청 2026-09-20 13:40 시험 테마", False),
    ('<script>예약번호 12345 예약상태 신청 2026-09-20 13:40 시험 테마</script>', False),
])
def test_receipt_requires_real_number_status_and_target(text, success):
    engine, context = engine_context()
    assert engine._receipt_result(text, context).success is success


@pytest.mark.parametrize("candidate", ["12345", "12 345", "54321", "1234", "12a45"])
def test_ocr_candidates_are_validated_not_guessed(monkeypatch, candidate):
    monkeypatch.setattr(captcha, "_recognize", lambda *_: candidate)
    stream = io.BytesIO()
    Image.new("RGB", (120, 50), "white").save(stream, "PNG")
    digits = asyncio.run(captcha.recognize_digits(stream.getvalue(), hashlib.md5(b"12345").hexdigest()))
    assert digits == ("12345" if candidate in {"12345", "12 345"} else "")


def test_same_weekday_estimate_has_no_historical_id(monkeypatch):
    from engines import time_slot_fetchers as fetchers
    dates = []
    class Response:
        def raise_for_status(self): pass
    def post(_url, data, **_kwargs):
        if data["act"] == "calendar":
            response = Response()
            response.content = b"fun_days_select('2026-09-13' fun_days_select('2026-09-06'"
            return response
        dates.append(data["rev_days"])
        response = Response()
        response.content = (b'' if len(dates) < 3 else
            b'<a href="javascript:fun_theme_time_select(\'OLD-ID\',\'0\')">13:40</a>')
        return response
    monkeypatch.setattr(fetchers.requests, "post", post)
    slots = fetch_any_time_slots({"engine_id":"zeroworld_shin", "base_url":"https://zero.example"}, "5", "9", "2026-09-20")
    assert dates == ["2026-09-20", "2026-09-13", "2026-09-06"]
    assert slots[0].estimated and not slots[0].available
    assert slots[0].source_date == "2026-09-06" and slots[0].slot_id == ""


def test_captcha_failure_stops_all_workers_without_orders(monkeypatch):
    engine, context = engine_context()
    from engines.zeroworld_catalog import ZeroWorldTimeSlot
    class Session:
        async def close(self): pass
    engine.session_pool = [(Session(), True, "SLOT")]
    monkeypatch.setattr(engine, "wait_async_scan_turn", AsyncMock())
    monkeypatch.setattr(engine, "_wait_for_date", AsyncMock(return_value=True))
    monkeypatch.setattr(engine, "_find_target_slot", AsyncMock(return_value=ZeroWorldTimeSlot("13:40", "SLOT", True)))
    prepare = AsyncMock(side_effect=ZeroWorldAuthenticationRequired("OCR unavailable"))
    monkeypatch.setattr(engine, "_prepare_captcha", prepare)
    async def run():
        engine.async_submission_lock = asyncio.Lock()
        await asyncio.gather(*(engine.make_reservation_async_task(dict(branch="5", reservationDate="2026-09-20", reservationTime="13:40", themePK="9"), i) for i in range(32)))
    asyncio.run(run())
    assert prepare.await_count == 1
    assert engine.stop_event.is_set()
    assert engine._final_submission_state == "authentication_required"


class Response:
    def __init__(self, text="", status=200):
        self.body = text.encode("utf-8")
        self.status = status
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return False
    async def read(self): return self.body


LOOKUP = """<div>예약 정보 테마 : 시험 테마 예약일시 : 2026년 9월 20일 (일) 13:40
인원 : 2명 테마금액 : 46,000원 결제금액 : 0원 결제방식 : 내방결제
진행상태 : 신청 <a>예약취소하기</a></div>"""


@pytest.mark.parametrize("text,success", [
    (LOOKUP, True),
    (LOOKUP.replace("신청", "환불"), False),
    (LOOKUP.replace("신청", "취소"), False),
    (LOOKUP.replace("20일", "21일"), False),
    (LOOKUP.replace("13:40", "13:41"), False),
    (LOOKUP.replace("시험 테마", "다른 테마"), False),
    (LOOKUP.replace("시험 테마", "시험 테마2"), False),
    (LOOKUP.replace("신청", "신청취소"), False),
    (LOOKUP.replace("2명", "3명"), False),
    (LOOKUP.replace("인원 : 2명", ""), False),
    (LOOKUP + " 예약번호 99999", False),
    ("9999", False),
])
def test_live_lookup_shape_requires_exact_booking_target(text, success):
    engine, context = engine_context()
    result = engine._receipt_result(text, context, lookup_number="12345")
    assert result.success is success
    if success:
        assert result.booking_number == "12345"
    if "예약번호" not in text:
        assert not engine._receipt_result(text, context).success


@pytest.mark.parametrize("failure", [False, True])
def test_receipt_lookup_is_read_only_and_timeout_is_uncertain(failure):
    engine, context = engine_context()
    posts = []
    class Session:
        def post(self, url, data, **_kwargs):
            posts.append((url, data))
            if failure:
                raise asyncio.TimeoutError()
            return Response(LOOKUP)
    result = asyncio.run(engine._confirm_booking(Session(), context, "12345"))
    assert result.success is not failure
    assert len(posts) == 1
    assert posts[0][1] == dict(act="rev_view", not_html="Y", name=context.name,
                             mobile=context.phone, ck_code="12345")


def test_payment_result_survives_local_history_failure(monkeypatch):
    import engines.zeroworld_shin_engine as module
    engine, context = engine_context()
    posts = []
    class Session:
        def post(self, url, data, **_kwargs):
            posts.append(url)
            return Response("예약번호 12345 예약상태 신청 2026-09-20 13:40 시험 테마")
    monkeypatch.setattr(module, "append_history", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(module.webbrowser, "open", lambda *_: False)
    result = asyncio.run(engine._complete_payment(Session(),
        '<input value="safe-code" type="hidden" name="code"><input value="12345" name="ck_code">',
        "", [], context))
    assert result.success and result.booking_number == "12345"
    assert posts == [engine.payment_url]


def test_unopened_calendar_blocks_phantom_slots_then_orders_once(monkeypatch):
    engine, context = engine_context()
    from engines.zeroworld_catalog import ZeroWorldTimeSlot
    engine.session_pool = [(object(), True, "")]
    monkeypatch.setattr(engine, "wait_async_scan_turn", AsyncMock())
    calendar = AsyncMock(side_effect=[False, False, True])
    find = AsyncMock(return_value=ZeroWorldTimeSlot("13:40", "LIVE-ID", True))
    submit = AsyncMock(return_value=BookingResult(True, "confirmed", "12345"))
    monkeypatch.setattr(engine, "_wait_for_date", calendar)
    monkeypatch.setattr(engine, "_find_target_slot", find)
    monkeypatch.setattr(engine, "_prepare_time_slot", AsyncMock(return_value=True))
    monkeypatch.setattr(engine, "_submit", submit)
    async def run():
        engine.async_submission_lock = asyncio.Lock()
        await asyncio.gather(*(engine.make_reservation_async_task(dict(branch="5", reservationDate="2026-09-20",
            reservationTime="13:40", themePK="9"), i) for i in range(32)))
    asyncio.run(run())
    assert calendar.await_count == 3 and find.await_count == 1 and submit.await_count == 1
    assert submit.await_args.args[2] == "LIVE-ID"
    assert engine._final_submission_state == "success"


@pytest.mark.parametrize("body,valid", [
    ("a" * 32, True),
    ("<b>Notice</b>: Undefined index: PHPSESSID<br />" + "a" * 32, True),
    ("<html>Login required</html>" + "a" * 32, False),
    ("<b>Notice</b>: Other warning<br />" + "a" * 32, False),
])
def test_only_identified_digest_response_is_accepted(body, valid):
    assert bool(captcha.parse_digest(body)) is valid


def test_numeric_ctc_preserves_repeated_digits_separated_by_blank():
    import numpy as np
    charset = ["", "1", "2", "3", "4", "X"]
    logits = np.full((7, 1, 6), -15.0)
    for step, index in enumerate([1, 1, 0, 1, 2, 3, 0]):
        logits[step, 0, index] = 15.0
    logits[:, 0, 5] = 30.0  # Non-numeric predictions must not remove digits.
    assert captcha._decode_candidates(logits, charset)[0] == "1123"


def test_missing_digest_never_accepts_unverified_ocr():
    with pytest.raises(ValueError):
        asyncio.run(captcha.recognize_digits(b"unused", ""))


def test_dense_digit_fallback_remains_image_based_and_digest_checked(monkeypatch):
    source = Image.new('RGB', (120, 60), (210, 220, 230))
    for x in range(15, 100):
        source.putpixel((x, 25), (x, x, x))
    original = io.BytesIO()
    source.save(original, 'PNG')
    narrow = io.BytesIO()
    source.resize((120, 30), Image.Resampling.BICUBIC).save(narrow, 'PNG')
    matched = []

    def recognize(raw, beta, width):
        if raw == narrow.getvalue() and width == 128:
            matched.append(beta)
            return ['54321', '12345']
        return []

    monkeypatch.setattr(captcha, '_recognize', recognize)
    result = asyncio.run(captcha.recognize_digits(
        original.getvalue(), hashlib.md5(b'12345').hexdigest()))
    assert result == '12345'
    assert matched == [False]


def test_model_assets_are_included_without_detector():
    from pathlib import Path
    from PyInstaller.utils.hooks import collect_data_files
    assets = collect_data_files("ddddocr", includes=["common.onnx", "common_old.onnx"])
    assert {Path(source).name for source, _ in assets} == {"common.onnx", "common_old.onnx"}
