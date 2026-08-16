from __future__ import annotations

import logging
import os
import re

from engines.base_engine import BaseEngine
from pengucro import logging_setup
from pengucro.diagnostics import (
    format_exception,
    redact_debug_text,
    write_redacted_debug_text,
)


class LoggingDummyEngine(BaseEngine):
    pass


def teardown_function():
    logging_setup._reset_configuration_for_tests()


def test_scrub_masks_registered_pii_and_common_auth_formats():
    logging_setup.register_secret("홍길동")
    source = (
        "name=홍길동 phone=010-1234-5678 email=user@example.com\n"
        "Authorization: Bearer abc.def.ghi\n"
        "Cookie: NID_SES=cookie-value; foo=bar\n"
        '{"api_key":"api-secret", "token": "captcha-secret"}\n'
        "https://example.test/path?access_token=query-secret&slot=2828"
    )

    redacted = logging_setup.scrub(source)

    for secret in (
        "홍길동",
        "010-1234-5678",
        "user@example.com",
        "abc.def.ghi",
        "cookie-value",
        "api-secret",
        "captcha-secret",
        "query-secret",
    ):
        assert secret not in redacted
    assert "slot=2828" in redacted
    assert redacted.count("[redacted]") >= 7


def test_scrub_masks_a_one_character_name_without_corrupting_other_words():
    logging_setup.register_secret("김")
    assert logging_setup.scrub("name=김 김치") == "name=[redacted] 김치"


def test_sensitive_mapping_registers_private_values_but_not_public_ids():
    logging_setup.register_sensitive_mapping(
        {
            "name": "예약자명",
            "phone": "01011112222",
            "yescaptcha_client_key": "client-secret",
            "themePK": "public-theme-42",
            "engine_metadata": {"session_id": "session-secret"},
            "cookies": {"NID_SES": "nested-cookie-secret"},
        }
    )

    result = logging_setup.scrub(
        "예약자명 01011112222 client-secret session-secret nested-cookie-secret public-theme-42"
    )

    assert "예약자명" not in result
    assert "01011112222" not in result
    assert "client-secret" not in result
    assert "session-secret" not in result
    assert "nested-cookie-secret" not in result
    assert "public-theme-42" in result


def test_process_log_persists_across_reconfiguration_with_context(tmp_path):
    first_path = logging_setup.configure(base_directory=tmp_path)
    run_id = logging_setup.begin_run()
    logging_setup.persist_log_line("DoomEscapeEngine", "첫 실행", "warning")
    logging_setup.persist_ui_lines([("UI 직접 기록", "info")])
    logging_setup._reset_configuration_for_tests()

    second_path = logging_setup.configure(base_directory=tmp_path)
    logging_setup.persist_log_line("DoomEscapeEngine", "두 번째 실행", "success")
    logging_setup._reset_configuration_for_tests()

    assert first_path == second_path
    assert first_path is not None
    assert re.fullmatch(rf"app-\d{{8}}-{os.getpid()}\.log", first_path.name)
    content = first_path.read_text(encoding="utf-8")
    assert "첫 실행" in content
    assert "UI 직접 기록" in content
    assert "두 번째 실행" in content
    assert "WARNING" in content
    assert "SUCCESS" in content
    assert "pengucro.runtime.DoomEscapeEngine" in content
    assert "pengucro.runtime.ui" in content
    assert f"pid={os.getpid()}" in content
    assert f"run={run_id}" in content


def test_base_engine_persists_with_engine_class_and_redacts_run_payload(tmp_path):
    path = logging_setup.configure(base_directory=tmp_path)
    logging_setup.replace_run_secrets(
        {
            "name": "민감이름",
            "phone": "010-2222-3333",
            "yescaptcha_client_key": "api-private-value",
        }
    )
    logging_setup.begin_run()
    visible = []
    engine = LoggingDummyEngine(lambda message, level: visible.append((message, level)))

    engine.log("민감이름 010-2222-3333 api-private-value 작업 시작", "info")
    logging_setup._reset_configuration_for_tests()

    assert visible and "민감이름" in visible[0][0]
    assert path is not None
    persisted = path.read_text(encoding="utf-8")
    assert "LoggingDummyEngine" in persisted
    assert "작업 시작" in persisted
    assert "민감이름" not in persisted
    assert "010-2222-3333" not in persisted
    assert "api-private-value" not in persisted


def test_redacting_formatter_covers_exception_text():
    formatter = logging_setup.RedactingFormatter("%(levelname)s %(message)s")
    try:
        raise RuntimeError("Authorization: Bearer unsafe-token")
    except RuntimeError:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "request failed",
            (),
            exc_info=__import__("sys").exc_info(),
        )

    formatted = formatter.format(record)
    assert "unsafe-token" not in formatted
    assert "RuntimeError" in formatted


def test_async_handler_performs_target_io_on_writer_thread():
    writes = []

    class MemoryTarget(logging.Handler):
        def emit(self, record):
            import threading

            writes.append((threading.current_thread().name, record.getMessage()))

    target = MemoryTarget(logging.INFO)
    handler = logging_setup.AsyncRotatingFileHandler(target)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "queued", (), None)
    handler.handle(record)
    handler.close()

    assert writes == [("PengucroLogWriter", "queued")]


def test_standard_logging_shutdown_flushes_the_async_queue(tmp_path):
    path = logging_setup.configure(base_directory=tmp_path)
    logging_setup.persist_log_line("ShutdownProbe", "종료 직전 대기 로그", "info")

    # app.py uses logging.shutdown() in its finally block. The custom handler's
    # close method must therefore drain its queue without an app-specific hook.
    logging.shutdown()

    assert path is not None
    assert "종료 직전 대기 로그" in path.read_text(encoding="utf-8")


def test_exception_formatter_is_useful_when_message_is_blank():
    assert format_exception(TimeoutError()) == "TimeoutError"

    class EmptyNetworkError(Exception):
        status_code = 503

    assert format_exception(EmptyNetworkError()) == "EmptyNetworkError (status_code=503)"


def test_debug_redaction_handles_attribute_order_textarea_and_atomic_write(tmp_path):
    source = (
        '<input value="홍길동" name="name">'
        '<input name="captchaToken" value="captcha-secret">'
        '<textarea id="phone">01012345678</textarea>'
        '<script>const config={"session_id":"session-secret"};</script>'
        '<input name="slot" value="2828">'
    )

    redacted = redact_debug_text(source, extra_secrets=("홍길동",))
    assert "홍길동" not in redacted
    assert "captcha-secret" not in redacted
    assert "01012345678" not in redacted
    assert "session-secret" not in redacted
    assert 'name="slot" value="2828"' in redacted

    target = write_redacted_debug_text(
        tmp_path / "nested" / "response.html",
        source,
        extra_secrets=("홍길동",),
    )
    persisted = target.read_text(encoding="utf-8")
    assert persisted == redacted
    assert not list(target.parent.glob("*.tmp"))
