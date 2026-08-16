from engines.server_clock import ServerClock


class MissingDateResponse:
    status_code = 503
    headers = {}


def test_sync_reports_exception_type_when_initial_read_fails():
    class FailingSession:
        @staticmethod
        def head(_url, timeout):
            del timeout
            raise TimeoutError()

    messages = []
    clock = ServerClock(
        "https://example.test",
        session=FailingSession(),
        log=lambda message, level="info": messages.append((message, level)),
    )

    assert clock.sync(announce=True) is False
    assert messages
    assert "TimeoutError" in messages[-1][0]
    assert messages[-1][1] == "warning"


def test_sync_reports_http_status_when_date_header_is_missing():
    class MissingDateSession:
        @staticmethod
        def head(_url, timeout):
            del timeout
            return MissingDateResponse()

    messages = []
    clock = ServerClock(
        "https://example.test",
        session=MissingDateSession(),
        log=lambda message, level="info": messages.append((message, level)),
    )

    assert clock.sync(announce=True) is False
    assert "HTTP 503" in messages[-1][0]
    assert "Date 헤더 없음" in messages[-1][0]


def test_recent_boundary_intervals_are_fused_to_tighter_precision():
    clock = ServerClock("https://example.test")

    clock._apply_boundary_interval(1000.0, 10.00, 10.10)
    first_precision = clock.last_precision
    clock._apply_boundary_interval(1001.0, 11.02, 11.08)

    assert 0.049 <= first_precision <= 0.051
    assert 0.029 <= clock.last_precision <= 0.031


def test_fresh_clock_snapshot_can_be_reused_by_another_process():
    first = ServerClock("https://example.test")
    first._apply_boundary_interval(1000.0, 10.00, 10.10)
    snapshot = first.snapshot()

    second = ServerClock("https://example.test")
    assert second.apply_snapshot(snapshot, max_age=5.0) is True
    assert second.synced is True
    assert second.last_precision == first.last_precision
