import asyncio

import pytest

import engines.zeroworld_shin_engine as zeroworld_shin
from engines.zeroworld_catalog import ZeroWorldTimeSlot
from engines.zeroworld_shin_engine import ZeroWorldShinEngine
from pengucro.models import BookingResult


def make_engine():
    return ZeroWorldShinEngine("", lambda *_args: None)


@pytest.fixture(autouse=True)
def mock_captcha_preparation(monkeypatch):
    # These tests exercise reservation transport; OCR has its own test module.
    async def prepared(*_args):
        return "12345"
    monkeypatch.setattr(ZeroWorldShinEngine, "_prepare_captcha", prepared)
    monkeypatch.setattr(ZeroWorldShinEngine, "_wait_for_date", prepared)


@pytest.mark.parametrize(
    ("branch", "subject"),
    [("1", "A"), ("2", "B"), ("4", "A"), ("5", "A")],
)
def test_current_zeroworld_branches_are_supported(branch, subject):
    context = make_engine()._build_context(
        {
            "branch": branch,
            "reservationDate": "2026-08-01",
            "reservationTime": "11:00:00",
            "themePK": "28",
            "name": "테스트",
            "phone": "01012345678",
            "people": "2",
        }
    )
    assert context.branch == branch
    assert context.subject == subject
    assert context.target_time == "11:00"
    assert context.phone == "010-1234-5678"


def test_unsupported_old_branch_is_rejected():
    with pytest.raises(ValueError, match="김포·강남·홍대·다이브 건대"):
        make_engine()._build_context({"branch": "99"})


def test_submission_acceptance_rejects_failure_alert():
    assert not make_engine()._submission_accepted(
        "<script>alert('이미 예약된 시간입니다.')</script>",
        "https://zeroworldkorea.com/layout/res/home.php?go=rev.kcp&code=abc",
        [],
    )
    assert make_engine()._submission_accepted(
        '<input name="code" value="abc"><form action="rev.make.mutong.php">',
        "https://zeroworldkorea.com/layout/res/home.php?go=rev.kcp&code=abc",
        [],
    )


class FakeResponse:
    def __init__(self, status=200, body=b"", url="https://zero.example/response", history=None):
        self.status = status
        self._body = body
        self.url = url
        self.history = history or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self):
        return self._body


class SequenceSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.posts = []

    def post(self, url, data=None, **kwargs):
        self.posts.append((url, data))
        return next(self.responses)


class GetSession:
    def __init__(self, response):
        self.response = response

    def get(self, _url, **_kwargs):
        return self.response


def test_blank_exception_is_identifiable_and_sensitive_query_values_are_redacted():
    assert ZeroWorldShinEngine._format_exception(asyncio.TimeoutError()) == "TimeoutError"

    message = ZeroWorldShinEngine._format_exception(
        RuntimeError("https://zero.example/pay?code=SECRET&mobile=01012345678")
    )

    assert message.startswith("RuntimeError:")
    assert "SECRET" not in message
    assert "01012345678" not in message


def test_slot_http_failure_log_has_worker_status_rtt_and_retry_reason():
    logs = []
    engine = ZeroWorldShinEngine(
        "https://zero.example",
        lambda message, level: logs.append((message, level)),
    )
    context = engine._build_context(
        {
            "branch": "1",
            "reservationDate": "2026-08-14",
            "reservationTime": "11:00:00",
            "themePK": "28",
            "name": "테스트",
            "phone": "01012345678",
            "people": "2",
        }
    )
    session = SequenceSession([FakeResponse(status=503, body=b"maintenance")])

    slot_id = asyncio.run(engine._find_slot(session, context, "작업 4"))

    assert slot_id == ""
    message = logs[-1][0]
    assert "[작업 4]" in message
    assert "슬롯 조회 응답" in message
    assert "HTTP 503" in message
    assert "RTT" in message
    assert "재시도" in message


def test_prestaged_submit_log_records_each_stage_and_acceptance_evidence(monkeypatch):
    logs = []
    engine = ZeroWorldShinEngine(
        "https://zero.example",
        lambda message, level: logs.append((message, level)),
    )
    context = engine._build_context(
        {
            "branch": "1",
            "reservationDate": "2026-08-14",
            "reservationTime": "11:00:00",
            "themePK": "28",
            "name": "테스트",
            "phone": "01012345678",
            "people": "2",
        }
    )
    session = SequenceSession(
        [
            FakeResponse(),
            FakeResponse(),
            FakeResponse(),
            FakeResponse(
                body=b'<form action="rev.make.mutong.php"><input name="code" value="secret"></form>',
                url="https://zero.example/rev.make.mutong.php",
            ),
        ]
    )

    async def fake_complete(*_args, **_kwargs):
        return BookingResult(True, "완료")

    monkeypatch.setattr(engine, "_complete_payment", fake_complete)
    assert asyncio.run(engine._prestage_session(session, context, "작업 2"))
    assert asyncio.run(
        engine._prepare_time_slot(session, "SLOT-17", "작업 2", "시간 선택 사전 준비")
    )
    result = asyncio.run(engine._submit(session, context, "SLOT-17", "작업 2"))

    assert result and result.success
    assert [data["act"] for _url, data in session.posts] == [
        "theme_list",
        "theme_select",
        "theme_time_select",
        "make",
    ]
    text = "\n".join(message for message, _level in logs)
    for stage in (
        "테마 목록 사전 준비",
        "테마 선택 사전 준비",
        "시간 선택 사전 준비",
        "예약 제출",
    ):
        assert stage in text
    assert "HTTP 200" in text
    assert "RTT" in text
    assert "슬롯 ID SLOT-17" in text
    assert "예약 제출 승인 경로 확인" in text
    assert "secret" not in text


def test_closed_slot_id_is_exposed_for_time_selection_prestaging():
    engine = make_engine()
    context = engine._build_context(
        {
            "branch": "1",
            "reservationDate": "2026-08-14",
            "reservationTime": "11:00:00",
            "themePK": "28",
            "name": "테스트",
            "phone": "01012345678",
            "people": "2",
        }
    )
    session = SequenceSession(
        [
            FakeResponse(
                body=(
                    b'<a class="disabled" '
                    b'href="javascript:fun_theme_time_select(\'SLOT-CLOSED\')">11:00</a>'
                )
            )
        ]
    )

    target = asyncio.run(engine._find_target_slot(session, context, "작업 1"))

    assert target is not None
    assert target.slot_id == "SLOT-CLOSED"
    assert target.available is False


def test_submit_fast_path_posts_only_final_reservation_action(monkeypatch):
    engine = make_engine()
    context = engine._build_context(
        {
            "branch": "1",
            "reservationDate": "2026-08-14",
            "reservationTime": "11:00:00",
            "themePK": "28",
            "name": "테스트",
            "phone": "01012345678",
            "people": "2",
        }
    )
    session = SequenceSession(
        [
            FakeResponse(
                body=b'<form action="rev.make.mutong.php"><input name="code" value="x"></form>',
                url="https://zero.example/rev.make.mutong.php",
            )
        ]
    )

    async def fake_complete(*_args, **_kwargs):
        return BookingResult(True, "완료")

    monkeypatch.setattr(engine, "_complete_payment", fake_complete)
    result = asyncio.run(engine._submit(session, context, "SLOT-17", "작업 1"))

    assert result and result.success
    assert len(session.posts) == 1
    assert session.posts[0][0] == engine.action_url
    assert session.posts[0][1]["act"] == "make"


def test_payment_timeout_after_acceptance_never_retries_reservation_post(monkeypatch):
    engine = make_engine()
    context = engine._build_context(
        {
            "branch": "1",
            "reservationDate": "2026-08-14",
            "reservationTime": "11:00:00",
            "themePK": "28",
            "name": "테스트",
            "phone": "01012345678",
            "people": "2",
        }
    )
    session = SequenceSession(
        [
            FakeResponse(
                body=b'<form action="rev.make.mutong.php"><input name="code" value="x"></form>',
                url="https://zero.example/rev.make.mutong.php",
            )
        ]
    )

    async def timeout_after_acceptance(*_args, **_kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(engine, "_complete_payment", timeout_after_acceptance)
    result = asyncio.run(engine._submit(session, context, "SLOT-17", "작업 1"))

    assert result and not result.success
    assert result.details["outcome"] == "uncertain"
    assert len(session.posts) == 1


def test_ambiguous_reconcile_rejects_bare_payment_route_without_booking_code():
    engine = make_engine()
    context = engine._build_context(
        {
            "branch": "1",
            "reservationDate": "2026-08-14",
            "reservationTime": "11:00:00",
            "themePK": "28",
            "name": "테스트",
            "phone": "01012345678",
            "people": "2",
        }
    )
    session = GetSession(
        FakeResponse(
            body=b"<html>payment page</html>",
            url="https://zero.example/layout/res/home.php?go=rev.kcp",
        )
    )

    result = asyncio.run(
        engine._reconcile_ambiguous_submit(session, context, "작업 1")
    )

    assert result is None


def test_ambiguous_final_submit_stops_all_workers_after_one_post(monkeypatch):
    engine = make_engine()
    final_posts = 0

    class Session:
        async def close(self):
            return None

    async def prestage(*_args):
        return True

    async def find(*_args):
        return ZeroWorldTimeSlot("11:00", "SLOT-17", True)

    async def prepare(*_args):
        return True

    async def submit(*_args):
        nonlocal final_posts
        final_posts += 1
        raise asyncio.TimeoutError()

    async def reconcile(*_args):
        return None

    monkeypatch.setattr(zeroworld_shin.aiohttp, "ClientSession", lambda **_kwargs: Session())
    monkeypatch.setattr(engine, "_prestage_session", prestage)
    monkeypatch.setattr(engine, "_find_target_slot", find)
    monkeypatch.setattr(engine, "_prepare_time_slot", prepare)
    monkeypatch.setattr(engine, "_submit", submit)
    monkeypatch.setattr(engine, "_reconcile_ambiguous_submit", reconcile)

    async def scenario():
        engine.async_submission_lock = asyncio.Lock()
        await asyncio.gather(
            engine.make_reservation_async_task(
                {
                    "branch": "1",
                    "reservationDate": "2026-08-14",
                    "reservationTime": "11:00:00",
                    "themePK": "28",
                    "name": "테스트",
                    "phone": "01012345678",
                    "people": "2",
                },
                0,
            ),
            engine.make_reservation_async_task(
                {
                    "branch": "1",
                    "reservationDate": "2026-08-14",
                    "reservationTime": "11:00:00",
                    "themePK": "28",
                    "name": "테스트",
                    "phone": "01012345678",
                    "people": "2",
                },
                1,
            ),
        )

    asyncio.run(scenario())
    assert final_posts == 1
    assert engine._final_submission_state == "uncertain"


def test_worker_skips_calendar_and_reuses_closed_slot_preselection(monkeypatch):
    engine = make_engine()
    calls = {"prestage": 0, "find": 0, "prepare": [], "submit": 0}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def close(self):
            return None

    async def fake_prestage(_session, _context, _worker_name):
        calls["prestage"] += 1
        return True

    async def fake_find(_session, _context, _worker_name):
        calls["find"] += 1
        return ZeroWorldTimeSlot("11:00", "SLOT-17", calls["find"] >= 2)

    async def fake_prepare(_session, slot_id, _worker_name, stage):
        calls["prepare"].append((slot_id, stage))
        return True

    async def fake_submit(_session, _context, slot_id, _worker_name):
        calls["submit"] += 1
        assert slot_id == "SLOT-17"
        return BookingResult(True, "완료")

    async def fail_date_poll(*_args, **_kwargs):
        return True  # The calendar must now prove the target date is open.

    monkeypatch.setattr(zeroworld_shin.aiohttp, "ClientSession", lambda **_kwargs: Session())
    monkeypatch.setattr(engine, "_prestage_session", fake_prestage)
    monkeypatch.setattr(engine, "_find_target_slot", fake_find)
    monkeypatch.setattr(engine, "_prepare_time_slot", fake_prepare)
    monkeypatch.setattr(engine, "_submit", fake_submit)
    monkeypatch.setattr(engine, "_wait_for_date", fail_date_poll)

    async def run_worker():
        engine.async_submission_lock = asyncio.Lock()
        await engine.make_reservation_async_task(
            {
                "branch": "1",
                "reservationDate": "2026-08-14",
                "reservationTime": "11:00:00",
                "themePK": "28",
                "name": "테스트",
                "phone": "01012345678",
                "people": "2",
            },
            0,
        )

    asyncio.run(run_worker())

    assert calls == {
        "prestage": 1,
        "find": 2,
        "prepare": [("SLOT-17", "시간 선택 사전 준비")],
        "submit": 1,
    }


def test_debug_file_contains_structure_summary_without_raw_sensitive_html(tmp_path, monkeypatch):
    monkeypatch.setattr(zeroworld_shin, "data_path", lambda filename: tmp_path / filename)
    body = """
    <html><head><title>예약자 홍길동 010-1234-5678</title></head><body>
      <form action="/pay?code=SECRET-CODE">
        <input name="name" value="홍길동">
        <input name="mobile" value="010-1234-5678">
        <input name="ck_code" value="SECRET-TOKEN">
      </form>
      <script>alert('계좌 123456789012'); location='rev.make.end';</script>
    </body></html>
    """

    target = ZeroWorldShinEngine._save_debug("safe-debug.html", body, "결제 결과")
    saved = target.read_text(encoding="utf-8")

    assert "제로월드 안전 진단 요약" in saved
    assert "응답 길이" in saved
    assert "SHA-256" in saved
    assert "SECRET-CODE" not in saved
    assert "SECRET-TOKEN" not in saved
    assert "홍길동" not in saved
    assert "010-1234-5678" not in saved
    assert "123456789012" not in saved
    assert "<input" not in saved
