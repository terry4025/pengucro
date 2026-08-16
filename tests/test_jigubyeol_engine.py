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
