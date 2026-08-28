import asyncio
import json

from engines.jigubyeol_engine import JigubyeolEngine


def make_engine(logs=None):
    captured = logs if logs is not None else []
    return JigubyeolEngine(
        lambda message, level: captured.append((message, level)),
        site_url="https://jigubyeol.example",
    )


class SyncResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")


class AsyncResponse:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def read(self):
        return self._text.encode("utf-8")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class AsyncSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def reservation_data():
    return {
        "branch": "1",
        "themePK": "4",
        "reservationDate": "2026-08-28",
        "reservationTime": "19:05:00",
        "name": "테스트",
        "phone": "010-0000-0000",
        "people": "2",
    }


def test_blank_exception_is_identifiable_and_sensitive_values_are_redacted():
    reservation_data = {"name": "홍길동", "phone": "01012345678"}

    assert JigubyeolEngine._format_exception(asyncio.TimeoutError()) == "TimeoutError"
    formatted = JigubyeolEngine._format_exception(
        RuntimeError("name=홍길동 phone=01012345678"), reservation_data
    )

    assert formatted.startswith("RuntimeError:")
    assert "홍길동" not in formatted
    assert "01012345678" not in formatted


def test_sync_error_log_has_worker_stage_http_rtt_and_no_pii():
    logs = []
    engine = make_engine(logs)
    reservation_data = {
        "reservationTime": "11:20:00",
        "name": "홍길동",
        "phone": "01012345678",
    }
    body = json.dumps(
        {"message": "예약자 홍길동의 전화번호 01012345678 입력 오류"},
        ensure_ascii=False,
    )

    engine.handle_error(
        SyncResponse(422, body),
        reservation_data,
        "최종 예약",
        worker_name="작업 3",
        rtt_ms=37.4,
    )

    message = logs[-1][0]
    assert "[작업 3]" in message
    assert "최종 예약 거절" in message
    assert "HTTP 422" in message
    assert "RTT 37ms" in message
    assert "재시도" in message
    assert "홍길동" not in message
    assert "01012345678" not in message


def test_async_error_log_has_worker_and_diagnostics_without_body_dump():
    logs = []
    engine = make_engine(logs)
    reservation_data = {
        "reservationTime": "18:40:00",
        "name": "테스트사용자",
        "phone": "010-2222-3333",
    }

    asyncio.run(
        engine.handle_error_async(
            AsyncResponse(503, "<html><body>일시적인 서버 장애</body></html>"),
            reservation_data,
            "시간 선택",
            worker_name="태스크 2",
            rtt_ms=812.2,
        )
    )

    message = logs[-1][0]
    assert "[태스크 2]" in message
    assert "시간 선택 거절" in message
    assert "HTTP 503" in message
    assert "RTT 812ms" in message
    assert "일시적인 서버 장애" in message
    assert "<html>" not in message


def test_success_message_does_not_include_account_amount_or_deadline():
    body = """
    <html><body>입금전
      <table>
        <tr><th>가상계좌</th><td>123-456-789012</td></tr>
        <tr><th>입금액</th><td>120000원</td></tr>
        <tr><th>기한</th><td>2026-08-14 12:00</td></tr>
        <tr><th>예약번호</th><td>R-2048</td></tr>
      </table>
    </body></html>
    """

    message = JigubyeolEngine._success_message(body)

    assert "가상계좌 임시 예약 완료" in message
    assert "R-2048" in message
    assert "123-456-789012" not in message
    assert "120000" not in message
    assert "2026-08-14" not in message


def test_payment_method_uses_fast_attribute_order_independent_parser():
    assert JigubyeolEngine._payment_method_from_html(
        '<input value="2" type="hidden" name="payment_method">'
    ) == "2"
    assert JigubyeolEngine._payment_method_from_html("<html>no hidden value</html>") == "1"


def test_completion_requires_a_real_booking_number():
    assert JigubyeolEngine._completion_evidence("<html>예약이 완료되었습니다</html>") == ""
    assert JigubyeolEngine._completion_evidence(
        "<table><tr><th>예약번호</th><td>J-2048</td></tr></table>"
    ) == "J-2048"


def test_final_success_owner_holds_lock_through_done_confirmation():
    async def scenario():
        engine = make_engine()
        final_posts = 0

        class Session(AsyncSession):
            def get(self, _url):
                return AsyncResponse(
                    200,
                    "<table><tr><th>예약번호</th><td>J-1</td></tr></table>",
                )

        async def prefetch(num_sessions, _reservation_data):
            engine.session_pool = [(Session(), f"csrf-{idx}") for idx in range(num_sessions)]

        async def select(*_args):
            return AsyncResponse(200, '<input name="payment_method" value="1">')

        async def submit(*_args):
            nonlocal final_posts
            final_posts += 1
            await asyncio.sleep(0.02)
            return AsyncResponse(200, "accepted")

        engine.pre_fetch_sessions_async = prefetch
        engine.submit_time_selection_async = select
        engine.submit_reservation_async = submit
        await engine.run_async_tasks(reservation_data(), 2)
        assert final_posts == 1
        assert engine._final_submission_state == "success"

    asyncio.run(scenario())


def test_ambiguous_final_timeout_stops_without_second_post():
    async def scenario():
        engine = make_engine()
        final_posts = 0

        class Session(AsyncSession):
            def get(self, _url):
                return AsyncResponse(200, "로그인 화면")

        async def prefetch(num_sessions, _reservation_data):
            engine.session_pool = [(Session(), f"csrf-{idx}") for idx in range(num_sessions)]

        async def select(*_args):
            return AsyncResponse(200, '<input name="payment_method" value="1">')

        async def submit(*_args):
            nonlocal final_posts
            final_posts += 1
            raise asyncio.TimeoutError()

        engine.pre_fetch_sessions_async = prefetch
        engine.submit_time_selection_async = select
        engine.submit_reservation_async = submit
        await engine.run_async_tasks(reservation_data(), 2)
        assert final_posts == 1
        assert engine._final_submission_state == "uncertain"

    asyncio.run(scenario())


def test_async_time_selection_stays_concurrent_but_final_reservation_is_serialized():
    async def scenario():
        engine = make_engine()
        active_submissions = 0
        max_active_submissions = 0
        completed = 0

        async def prefetch(num_sessions, _reservation_data):
            engine.session_pool = [(AsyncSession(), f"csrf-{idx}") for idx in range(num_sessions)]

        async def submit_time_selection(_session, _csrf_token, _reservation_data):
            return AsyncResponse(200, '<input name="payment_method" value="1">')

        async def submit_reservation(_session, _csrf_token, _reservation_data, _payment_method):
            nonlocal active_submissions, max_active_submissions, completed
            active_submissions += 1
            max_active_submissions = max(max_active_submissions, active_submissions)
            await asyncio.sleep(0.12)
            active_submissions -= 1
            completed += 1
            if completed >= 2:
                engine.stop_event.set()
            return AsyncResponse(419, "CSRF expired")

        async def refresh_csrf(_session, _worker_name):
            return "refreshed-csrf"

        engine.pre_fetch_sessions_async = prefetch
        engine.submit_time_selection_async = submit_time_selection
        engine.submit_reservation_async = submit_reservation
        engine.get_csrf_token_async = refresh_csrf

        await engine.run_async_tasks(reservation_data(), 2)

        assert max_active_submissions == 1

    asyncio.run(scenario())


def test_async_csrf_refresh_uses_bounded_parallel_recovery():
    async def scenario():
        engine = make_engine()
        active_refreshes = 0
        max_active_refreshes = 0
        submissions = 0

        async def prefetch(num_sessions, _reservation_data):
            engine.session_pool = [(AsyncSession(), None) for _ in range(num_sessions)]

        async def refresh_csrf(_session, _worker_name):
            nonlocal active_refreshes, max_active_refreshes
            active_refreshes += 1
            max_active_refreshes = max(max_active_refreshes, active_refreshes)
            await asyncio.sleep(0.02)
            active_refreshes -= 1
            return "refreshed-csrf"

        async def submit_time_selection(_session, _csrf_token, _reservation_data):
            nonlocal submissions
            submissions += 1
            if submissions >= 2:
                engine.stop_event.set()
            return AsyncResponse(419, "CSRF expired")

        engine.pre_fetch_sessions_async = prefetch
        engine.get_csrf_token_async = refresh_csrf
        engine.submit_time_selection_async = submit_time_selection

        await engine.run_async_tasks(reservation_data(), 2)

        assert max_active_refreshes == 2

    asyncio.run(scenario())


def test_error_html_is_parsed_once_while_every_attempt_is_still_logged(monkeypatch):
    logs = []
    engine = make_engine(logs)
    parses = 0
    original = engine._safe_error_message

    def counted(body, data=None):
        nonlocal parses
        parses += 1
        return original(body, data)

    monkeypatch.setattr(engine, "_safe_error_message", counted)
    response_body = "<html><body>아직 예약할 수 없습니다.</body></html>"

    asyncio.run(
        engine.handle_error_async(
            AsyncResponse(422, response_body),
            reservation_data(),
            "시간 선택",
        )
    )
    asyncio.run(
        engine.handle_error_async(
            AsyncResponse(422, response_body),
            reservation_data(),
            "시간 선택",
        )
    )

    assert parses == 1
    assert len(logs) == 2


def test_configured_fifty_workers_can_all_monitor_time_selection_concurrently():
    async def scenario():
        engine = make_engine()
        active = 0
        peak = 0
        all_started = asyncio.Event()

        async def prefetch(num_sessions, _reservation_data):
            engine.session_pool = [
                (AsyncSession(), f"csrf-{idx}") for idx in range(num_sessions)
            ]

        async def submit_time_selection(_session, _csrf_token, _reservation_data):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 50:
                all_started.set()
                engine.stop_event.set()
            await asyncio.wait_for(all_started.wait(), timeout=1.0)
            active -= 1
            return AsyncResponse(419, "CSRF expired")

        engine.pre_fetch_sessions_async = prefetch
        engine.submit_time_selection_async = submit_time_selection

        await asyncio.wait_for(
            engine.run_async_tasks(reservation_data(), 50),
            timeout=2.0,
        )
        return peak

    assert asyncio.run(scenario()) == 50
