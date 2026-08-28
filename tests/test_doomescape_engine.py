import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from engines.doomescape_engine import (
    DoomEscapeEngine,
    DoomOrderNotSent,
    DoomScanGovernor,
    DoomSubmissionUncertain,
)


def test_empty_timeout_error_has_a_useful_name():
    assert DoomEscapeEngine._describe_exception(asyncio.TimeoutError()) == "TimeoutError"


def test_exception_diagnostic_redacts_doom_completion_code():
    described = DoomEscapeEngine._describe_exception(
        RuntimeError("GET https://doomescape.com/rev?num=12&ck_code=SECRET-CODE failed")
    )

    assert "SECRET-CODE" not in described
    assert "ck_code=[redacted]" in described


def test_session_prefetch_caps_scan_connections_and_keeps_submit_session(monkeypatch):
    import aiohttp

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def read(self):
            await asyncio.sleep(0.05)
            return b"ok"

    class FakeSession:
        def __init__(self, **_kwargs):
            self.closed = False

        def get(self, *_args, **_kwargs):
            return FakeResponse()

        async def close(self):
            self.closed = True

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    engine = DoomEscapeEngine("https://doomescape.com", lambda *_args: None)

    started = time.perf_counter()
    asyncio.run(engine.pre_fetch_sessions_async(50, {}))
    elapsed = time.perf_counter() - started

    assert engine._scan_session_count == 8
    assert len(engine.session_pool) == 10
    assert engine._submit_session is engine.session_pool[-2]
    assert engine._submit_hedge_session is engine.session_pool[-1]
    assert elapsed < 0.20


def test_active_outage_keeps_a_fast_probe_floor_without_a_global_gate():
    governor = DoomScanGovernor(2, 12, 1, 6)
    governor.set_phase("active")

    for _ in range(20):
        governor.observe_failure()

    assert governor.target_rate == 6
    assert not hasattr(DoomEscapeEngine, "_wait_for_site_recovery")
    assert (
        DoomEscapeEngine.ACTIVE_SCAN_RATE_PER_SECOND
        * DoomEscapeEngine.LIST_TIMEOUT_SECONDS
        <= DoomEscapeEngine.MAX_SCAN_INFLIGHT
    )
    slow_fraction = 1 / DoomEscapeEngine.SLOW_LIST_EVERY_N_TASKS
    expected_inflight = DoomEscapeEngine.ACTIVE_SCAN_RATE_PER_SECOND * (
        (1 - slow_fraction) * DoomEscapeEngine.LIST_TIMEOUT_SECONDS
        + slow_fraction * DoomEscapeEngine.SLOW_LIST_TIMEOUT_SECONDS
    )
    assert expected_inflight <= DoomEscapeEngine.MAX_SCAN_INFLIGHT


def test_list_timeout_keeps_fast_lane_and_periodic_slow_recovery_lane():
    observed = [DoomEscapeEngine._list_timeout_for_task(index) for index in range(8)]

    assert observed == [4.5, 1.25, 1.25, 1.25, 4.5, 1.25, 1.25, 1.25]


def test_governor_does_not_reserve_idle_dispatches_far_ahead():
    async def run():
        governor = DoomScanGovernor(2, 20, 1, 5)
        started = time.monotonic()
        tasks = [asyncio.create_task(governor.wait_turn()) for _ in range(3)]
        await tasks[0]
        governor.set_phase("active")
        await asyncio.gather(*tasks[1:])
        return time.monotonic() - started

    assert asyncio.run(run()) < 0.8


def test_open_anchor_stays_active_across_midnight_and_after_open():
    before_open = datetime(2026, 8, 28, 23, 44, 50)
    after_open = datetime(2026, 8, 28, 23, 50, 0)
    after_midnight = datetime(2026, 8, 29, 0, 5, 0)

    assert datetime.fromtimestamp(
        DoomEscapeEngine._open_anchor_from_wall_clock("23:45:00", before_open)
    ) == datetime(2026, 8, 28, 23, 45, 0)
    assert datetime.fromtimestamp(
        DoomEscapeEngine._open_anchor_from_wall_clock("23:45:00", after_open)
    ) == datetime(2026, 8, 28, 23, 45, 0)
    assert datetime.fromtimestamp(
        DoomEscapeEngine._open_anchor_from_wall_clock("23:45:00", after_midnight)
    ) == datetime(2026, 8, 28, 23, 45, 0)


def test_failure_diagnostic_persists_metadata_without_raw_html(tmp_path, monkeypatch):
    logs = []
    monkeypatch.chdir(tmp_path)
    engine = DoomEscapeEngine(
        "https://doomescape.com",
        lambda message, level: logs.append((message, level)),
    )
    raw = (
        '<html><input name="name" value="홍길동">'
        '<input name="phone" value="010-1234-5678">'
        '<input name="ck_code" value="SECRET-TOKEN">'
        '<meta http-equiv="refresh" content="0;url=rev.make.exe.php">완료</html>'
    )

    engine._write_safe_failure_summary(
        worker="태스크 2",
        stage="무통장 예약 결과 판정",
        status=200,
        response_text=raw,
        slot_id="34",
        order_id="9001",
    )

    target = tmp_path / "scratch" / "last_mutong_diagnostic.json"
    saved_text = target.read_text(encoding="utf-8")
    payload = json.loads(saved_text)

    assert payload["worker"] == "태스크 2"
    assert payload["http_status"] == 200
    assert payload["slot_id"] == "34"
    assert payload["order_id"] == "9001"
    assert payload["markers"]["has_completion_marker"] is True
    assert "홍길동" not in saved_text
    assert "010-1234-5678" not in saved_text
    assert "SECRET-TOKEN" not in saved_text
    assert "<html" not in saved_text
    assert any("민감정보를 제외한 요약" in message for message, _level in logs)


def test_http_diagnostic_includes_worker_stage_status_and_rtt():
    logs = []
    engine = DoomEscapeEngine(
        "https://doomescape.com",
        lambda message, level: logs.append((message, level)),
    )

    engine._log_http_diagnostic(
        "태스크 3", "예약 주문 생성", "POST", 503, 0.123, force=True
    )

    message, level = logs[-1]
    assert "[태스크 3]" in message
    assert "예약 주문 생성" in message
    assert "status=503" in message
    assert "RTT 123ms" in message
    assert level == "warning"


def test_timetable_diagnostics_are_aggregated_across_workers():
    logs = []
    engine = DoomEscapeEngine(
        "https://doomescape.com",
        lambda message, level: logs.append((message, level)),
    )

    engine._log_http_diagnostic("태스크 1", "시간표 조회", "GET", 200, 0.1)
    engine._log_http_diagnostic("태스크 2", "시간표 조회", "GET", 200, 0.1)

    assert len(logs) == 1
    assert "[전체 감시]" in logs[0][0]


def test_missing_slot_is_classified_as_unopened_or_sold_out():
    unopened = "<html><body>오픈 전</body></html>"
    sold_out = (
        "<div class='tm_box'><p class='name'>나폴리탄</p>"
        '<a><span class="num">14:00</span>'
        '<span class="txt">예약마감</span></a></div>'
    )

    assert DoomEscapeEngine._classify_missing_slot(unopened, "나폴리탄") == "미오픈"
    assert (
        DoomEscapeEngine._classify_missing_slot(sold_out, "나폴리탄")
        == "오픈됨·해당 시간 없음"
    )


def test_timetable_rejects_navigation_date_when_slots_are_for_another_day():
    html = (
        '<a href="?go=rev.make&rev_days=2026-09-05">다음 날짜</a>'
        '<div class="tm_box"><p class="name">데이투어</p>'
        '<a href="?go=rev.make.input&rev_days=2026-08-29&theme_time_num=36">'
        '<span class="num">19:00</span><span class="txt">예약가능</span>'
        "</a></div>"
    )

    analysis = DoomEscapeEngine.analyze_timetable(
        html, "데이투어", "2026-09-05", "19:00"
    )

    assert analysis["page_dates"] == ["2026-08-29"]
    assert analysis["date_matches"] is False
    assert analysis["slot_id"] is None
    assert analysis["reason"] == "날짜 미공개"


def test_timetable_fails_closed_when_slot_date_is_unknown():
    html = (
        '<div class="tm_box"><p class="name">데이투어</p>'
        '<a href="?go=rev.make.input&theme_time_num=36">'
        '<span class="num">19:00</span><span class="txt">예약가능</span>'
        "</a></div>"
    )

    analysis = DoomEscapeEngine.analyze_timetable(
        html, "데이투어", "2026-09-05", "19:00"
    )

    assert analysis["date_verified"] is False
    assert analysis["date_matches"] is False
    assert analysis["target_date_verified"] is False
    assert analysis["slot_id"] is None
    assert analysis["reason"] == "날짜 미공개"


def test_timetable_validates_the_target_anchor_date_not_another_theme_date():
    html = (
        '<div class="tm_box"><p class="name">다른테마</p>'
        '<a href="?rev_days=2026-09-05&theme_time_num=99">'
        '<span class="num">19:00</span><span class="txt">예약가능</span></a></div>'
        '<div class="tm_box"><p class="name">데이투어</p>'
        '<a href="?rev_days=2026-09-04&theme_time_num=36">'
        '<span class="num">19:00</span><span class="txt">예약가능</span></a></div>'
    )

    analysis = DoomEscapeEngine.analyze_timetable(
        html, "데이투어", "2026-09-05", "19:00"
    )

    assert analysis["date_matches"] is True
    assert analysis["target_date_verified"] is False
    assert analysis["slot_id"] is None
    assert analysis["reason"] == "날짜 미공개"


def test_timetable_accepts_only_exact_time_on_the_target_date():
    html = (
        '<div class="tm_box"><p class="name">데이투어</p>'
        '<a href="?rev_days=2026-09-05&theme_time_num=35">'
        '<span class="num">9:00</span><span class="txt">예약가능</span></a>'
        '<a href="?rev_days=2026-09-05&theme_time_num=36">'
        '<span class="num">19:00</span><span class="txt">예약가능</span></a></div>'
    )

    morning = DoomEscapeEngine.analyze_timetable(
        html, "데이투어", "2026-09-05", "09:00"
    )
    evening = DoomEscapeEngine.analyze_timetable(
        html, "데이투어", "2026-09-05", "19:00"
    )

    assert morning["slot_id"] == "35"
    assert evening["slot_id"] == "36"


def test_price_fields_ignore_attribute_order_and_capacity_rejection_is_not_reposted():
    html = (
        '<input value="126000" name="price" type="hidden">'
        '<input name="price3" type="hidden" value="168000">'
        '<input type="text" name="price4" value="999999">'
    )

    assert DoomEscapeEngine._extract_price_fields(html) == {
        "price": "126000",
        "price3": "168000",
    }
    assert DoomEscapeEngine._prestage_rejection_requires_refresh(
        200, "<script>alert('선택하신 시간은 이미 예약 마감되었습니다')</script>"
    ) is False
    assert DoomEscapeEngine._prestage_rejection_requires_refresh(
        200, "<script>alert('가격 정보가 잘못되었습니다')</script>"
    ) is True


def test_order_id_parser_does_not_confuse_theme_time_num_with_order_num():
    rejected = (
        '<a href="?go=rev.make.input&rev_days=2026-09-05'
        '&theme_time_num=36">다시 입력</a>'
    )

    assert DoomEscapeEngine._extract_order_id(rejected) == ""
    assert DoomEscapeEngine._extract_order_id("location.href='?num=9001'") == "9001"
    assert DoomEscapeEngine._extract_order_id(
        '<input value="9002" type="hidden" name="num">'
    ) == "9002"


def test_price_validation_never_falls_back_to_hardcoded_amounts():
    with pytest.raises(RuntimeError, match="INVALID_PRICE_FIELDS"):
        DoomEscapeEngine._validated_price_fields({"price": "126000"}, "3")

    assert DoomEscapeEngine._validated_price_fields(
        {"price": "46000", "price2": "46000", "price3": "66000"}, "3"
    ) == {"price": "66000", "price2": "46000", "price3": "66000"}


def test_async_worker_claims_on_first_recovered_timetable(monkeypatch):
    import engines.doomescape_engine as module
    import webbrowser

    target_html = (
        '<div class="tm_box"><p class="name">데이투어</p>'
        '<a href="?rev_days=2026-09-05&theme_time_num=36">'
        '<span class="num">19:00</span><span class="txt">예약가능</span>'
        "</a></div>"
    )

    class Response:
        def __init__(self, body, status=200):
            self.body = body.encode("utf-8")
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def read(self):
            return self.body

        async def text(self, **_kwargs):
            return self.body.decode("utf-8")

    class TimeoutResponse:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *_args):
            return False

    class ScanSession:
        def __init__(self):
            self.get_count = 0

        def get(self, *_args, **_kwargs):
            self.get_count += 1
            if self.get_count == 1:
                return TimeoutResponse()
            return Response(target_html)

        async def close(self):
            return None

    class SubmitSession:
        def __init__(self):
            self.post_count = 0

        def get(self, url, **_kwargs):
            if "go=rev.make.input" in url:
                return Response('<input type="hidden" name="price3" value="126000">')
            if "go=rev.kcp" in url:
                return Response('<input name="ck_code" value="777">')
            if "rev.make.mutong.php" in url:
                return Response(
                    '<meta http-equiv="refresh" content="0;url=rev.make.exe.php?ck_code=888">'
                )
            return Response("예약 완료")

        def post(self, *_args, **_kwargs):
            self.post_count += 1
            return Response("<script>location.href='?num=9001'</script>")

        async def close(self):
            return None

    scan_session = ScanSession()
    submit_session = SubmitSession()
    successes = []
    logs = []
    engine = DoomEscapeEngine(
        "https://doomescape.com",
        lambda message, level: logs.append((message, level)),
        lambda: successes.append(True),
    )
    engine.scan_governor = DoomScanGovernor(1000, 1000, 1000, 1000)
    engine.scan_governor.set_phase("active")
    engine._scan_inflight = asyncio.Semaphore(2)
    engine._scan_session_count = 1
    engine._prestage_lock = asyncio.Lock()
    engine._slot_wait_started_at = time.time()
    engine.session_pool = [scan_session, submit_session]
    engine._submit_session = submit_session
    async def forbidden_prestage(*_args, **_kwargs):
        raise AssertionError("공개된 목표 슬롯보다 사전 준비를 먼저 기다리면 안 됩니다")

    engine._prestage_prices = forbidden_prestage
    monkeypatch.setattr(module, "append_history", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(webbrowser, "open", lambda *_args, **_kwargs: False)

    reservation = {
        "branch": "4",
        "reservationDate": "2026-09-05",
        "reservationTime": "19:00",
        "themePK": "36",
        "themeLabel": "데이투어",
        "name": "테스트",
        "phone": "010-1234-5678",
        "people": "3",
    }

    asyncio.run(engine.make_reservation_async_task(reservation, 0))

    assert scan_session.get_count == 2
    assert submit_session.post_count == 1
    assert successes == [True]
    assert any("전체 정지 없이 독립 감시" in message for message, _level in logs)
    assert any("예약 최종 완료" in message for message, _level in logs)


def test_async_order_timeout_stops_without_duplicate_post(monkeypatch):
    class Response:
        def __init__(self, body, status=200):
            self.body = body.encode("utf-8")
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def read(self):
            return self.body

        async def text(self, **_kwargs):
            return self.body.decode("utf-8")

    class TimeoutResponse:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *_args):
            return False

    target_html = (
        '<input type="hidden" name="rev_days" value="2026-09-05">'
        '<div class="tm_box"><p class="name">데이투어</p>'
        '<a href="?rev_days=2026-09-05&theme_time_num=36">'
        '<span class="num">19:00</span><span class="txt">예약가능</span>'
        "</a></div>"
    )

    class ScanSession:
        def get(self, *_args, **_kwargs):
            return Response(target_html)

    class SubmitSession:
        def __init__(self):
            self.post_count = 0

        def get(self, url, **_kwargs):
            assert "go=rev.make.input" in url
            return Response('<input type="hidden" name="price3" value="126000">')

        def post(self, *_args, **_kwargs):
            self.post_count += 1
            return TimeoutResponse()

    scan_session = ScanSession()
    submit_session = SubmitSession()
    logs = []
    engine = DoomEscapeEngine(
        "https://doomescape.com",
        lambda message, level: logs.append((message, level)),
    )
    engine.scan_governor = DoomScanGovernor(1000, 1000, 1000, 1000)
    engine.scan_governor.set_phase("active")
    engine._scan_inflight = asyncio.Semaphore(2)
    engine._scan_session_count = 1
    engine._prestage_lock = asyncio.Lock()
    engine._slot_wait_started_at = time.time()
    engine.session_pool = [scan_session, submit_session]
    engine._submit_session = submit_session
    monkeypatch.setattr(engine, "_write_safe_failure_summary", lambda **_kwargs: None)

    reservation = {
        "branch": "4",
        "reservationDate": "2026-09-05",
        "reservationTime": "19:00",
        "themePK": "36",
        "themeLabel": "데이투어",
        "name": "테스트",
        "phone": "010-1234-5678",
        "people": "3",
    }

    asyncio.run(engine.make_reservation_async_task(reservation, 0))

    assert submit_session.post_count == 1
    assert engine.stop_event.is_set()
    assert any("중복 방지 정지" in message for message, _level in logs)


def test_order_connect_failure_before_send_uses_warmed_fallback_once():
    import aiohttp

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def read(self):
            return b"order-num=9001"

    class PrimarySession:
        def __init__(self):
            self.post_count = 0

        def post(self, *_args, **_kwargs):
            self.post_count += 1
            raise aiohttp.ClientConnectionError("connect failed before send")

    class FallbackSession:
        def __init__(self):
            self.post_count = 0

        def post(self, *_args, **_kwargs):
            self.post_count += 1
            return Response()

    logs = []
    engine = DoomEscapeEngine(
        "https://doomescape.com",
        lambda message, level: logs.append((message, level)),
    )
    primary = PrimarySession()
    fallback = FallbackSession()
    engine._transport_traced_session_ids.update((id(primary), id(fallback)))

    status, body, used_session = asyncio.run(
        engine._post_order_with_safe_connect_retry_async(
            primary,
            fallback,
            "https://doomescape.com/core/res/rev.act.php",
            b"act=make",
            {"Content-Type": "application/x-www-form-urlencoded"},
            "태스크 1",
            "36",
        )
    )

    assert status == 200
    assert body == "order-num=9001"
    assert used_session is fallback
    assert primary.post_count == 1
    assert fallback.post_count == 1
    assert any("연결 즉시 전환" in message for message, _level in logs)


def test_transport_trace_marks_headers_as_sent():
    engine = DoomEscapeEngine("https://doomescape.com", lambda *_args: None)
    trace_config = engine._build_transport_trace_config()
    state = {"request_bytes_sent": False}
    context = SimpleNamespace(
        trace_request_ctx={"doom_transport_state": state}
    )

    asyncio.run(trace_config.on_request_headers_sent[0](None, context, None))

    assert state["request_bytes_sent"] is True


def test_order_failure_after_headers_sent_never_uses_fallback():
    class TimeoutResponse:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *_args):
            return False

    class SentSession:
        def __init__(self):
            self.post_count = 0

        def post(self, *_args, **kwargs):
            self.post_count += 1
            kwargs["trace_request_ctx"]["doom_transport_state"][
                "request_bytes_sent"
            ] = True
            return TimeoutResponse()

    class ForbiddenFallback:
        def __init__(self):
            self.post_count = 0

        def post(self, *_args, **_kwargs):
            self.post_count += 1
            raise AssertionError("전송 후에는 두 번째 주문 POST를 보내면 안 됩니다")

    engine = DoomEscapeEngine("https://doomescape.com", lambda *_args: None)
    primary = SentSession()
    fallback = ForbiddenFallback()
    engine._transport_traced_session_ids.update((id(primary), id(fallback)))

    with pytest.raises(DoomSubmissionUncertain, match="예약 주문 생성"):
        asyncio.run(
            engine._post_order_with_safe_connect_retry_async(
                primary,
                fallback,
                "https://doomescape.com/core/res/rev.act.php",
                b"act=make",
                {"Content-Type": "application/x-www-form-urlencoded"},
                "태스크 1",
                "36",
            )
        )

    assert primary.post_count == 1
    assert fallback.post_count == 0


def test_two_before_send_failures_remain_safe_to_retry():
    import aiohttp

    class FailedSession:
        def __init__(self):
            self.post_count = 0

        def post(self, *_args, **_kwargs):
            self.post_count += 1
            raise aiohttp.ClientConnectionError("connect failed before send")

    engine = DoomEscapeEngine("https://doomescape.com", lambda *_args: None)
    primary = FailedSession()
    fallback = FailedSession()
    engine._transport_traced_session_ids.update((id(primary), id(fallback)))

    with pytest.raises(DoomOrderNotSent, match="안전하게 다시 시도"):
        asyncio.run(
            engine._post_order_with_safe_connect_retry_async(
                primary,
                fallback,
                "https://doomescape.com/core/res/rev.act.php",
                b"act=make",
                {"Content-Type": "application/x-www-form-urlencoded"},
                "태스크 1",
                "36",
            )
        )

    assert primary.post_count == 1
    assert fallback.post_count == 1


def test_known_order_recovery_reads_completion_without_resending_confirmation():
    class Response:
        def __init__(self, body, status=200):
            self.body = body.encode("utf-8")
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def read(self):
            return self.body

    class TimeoutResponse:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *_args):
            return False

    class Session:
        def __init__(self):
            self.get_count = 0
            self.urls = []

        def get(self, url, **_kwargs):
            self.get_count += 1
            self.urls.append(url)
            if self.get_count == 1:
                return TimeoutResponse()
            return Response("예약 완료 · rev.make.end")

    engine = DoomEscapeEngine("https://doomescape.com", lambda *_args: None)
    engine.ORDER_RECOVERY_DELAY_SECONDS = 0
    session = Session()

    recovered = asyncio.run(
        engine._recover_known_order_async(session, "9001", "777", {})
    )

    assert recovered == (200, "예약 완료 · rev.make.end")
    assert session.get_count == 2
    assert all("go=rev.make.end" in url for url in session.urls)


def test_ui_payload_reaches_registry_and_base_async_start(monkeypatch):
    from engines.registry import EngineRegistry
    from pengucro.models import ReservationRequest, STANDARD_MODE

    request = ReservationRequest.from_mapping(
        "둠이스케이프",
        {
            "branch": "4",
            "reservationDate": "2026-09-05",
            "reservationTime": "19:00",
            "themePK": "36",
            "themeLabel": "데이투어",
            "name": "테스트",
            "phone": "010-1234-5678",
            "people": "3",
            "site_url": "https://doomescape.com",
        },
    )
    payload = request.to_engine_payload()
    engine = EngineRegistry.create(
        site_name="둠이스케이프",
        mode=STANDARD_MODE,
        payload=payload,
        custom_sites={},
        log_callback=lambda *_args: None,
        success_callback=lambda: None,
    )
    captured = {}

    async def capture_run(data, workers, *_args, **_kwargs):
        captured.update(payload=data, workers=workers)

    monkeypatch.setattr(engine, "run_async_tasks", capture_run)
    engine.start_reservation(payload, 50, is_async=True)
    deadline = time.time() + 2
    while engine.is_running and time.time() < deadline:
        time.sleep(0.01)

    assert isinstance(engine, DoomEscapeEngine)
    assert captured["workers"] == 50
    assert captured["payload"]["reservationDate"] == "2026-09-05"
    assert captured["payload"]["reservationTime"] == "19:00:00"
    assert captured["payload"]["themePK"] == "36"
    assert captured["payload"]["people"] == "3"
    assert engine.is_running is False


def test_missing_slot_log_throttles_and_reports_wait_time():
    logs = []
    engine = DoomEscapeEngine(
        "https://doomescape.com",
        lambda message, level: logs.append((message, level)),
    )
    engine._slot_wait_started_at = time.time() - 125

    engine._log_missing_slot("태스크 1", "14:00", "미오픈")
    engine._log_missing_slot("태스크 2", "14:00", "미오픈")

    assert len(logs) == 1
    message, level = logs[0]
    assert "[대기]" in message
    assert "미오픈" in message
    assert "대기 125초" in message
    assert level == "info"
