from engines.cgv_engine import CgvEngine as BaseCgvEngine
from engines.cgv_engine_guarded import CgvEngine
from engines.registry import EngineRegistry


class _Page:
    context = None

    def is_closed(self):
        return False


def _make_engine(logs):
    engine = CgvEngine(lambda message, level="info": logs.append((message, level)))
    engine._browser_auth_data = lambda _page: {}
    engine._seat_url = lambda _schedule, _cust_no="": "https://cgv.example/seat"
    engine._direct_hold_config = lambda *_args, **_kwargs: {}
    engine._start_fast_seat_monitor = lambda *_args, **_kwargs: True
    engine._stop_fast_seat_monitor = lambda _page: None
    return engine


def test_registry_uses_guarded_cgv_engine():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode="",
        payload={},
        custom_sites={},
        log_callback=lambda *_args: None,
        success_callback=None,
    )

    assert isinstance(engine, CgvEngine)
    assert engine.FAST_SEAT_LAUNCH_INTERVAL_MS == 350
    assert engine.FAST_SEAT_MAX_INFLIGHT == 1


def test_rate_limit_falls_back_immediately_without_exponential_backoff(monkeypatch):
    logs = []
    reads = []
    starts = []
    engine = _make_engine(logs)
    engine.scan_concurrency = 4
    engine._start_fast_seat_monitor = (
        lambda _page, _url, _groups, concurrency, **kwargs:
        starts.append((concurrency, kwargs["launch_interval_ms"])) or True
    )

    def blocked_snapshot(_page):
        reads.append(1)
        return {
            "running": False,
            "attempts": 4,
            "completed": 4,
            "inflight": 0,
            "consecutiveErrors": 0,
            "lastStatus": 429,
            "blocked": True,
            "unauthorized": False,
            "lastError": "HTTP 429",
            "terminalError": "",
            "conflicts": 0,
            "hit": None,
        }

    monkeypatch.setattr(
        BaseCgvEngine,
        "_read_fast_seat_monitor",
        staticmethod(blocked_snapshot),
    )

    held, fallback = engine._watch_and_hold_api(
        _Page(),
        {"siteNo": "0013", "scnYmd": "20260826"},
        (),
        2,
        False,
        {},
    )

    assert (held, fallback) == (False, True)
    assert len(reads) == 1
    assert starts == [(1, 350)]
    assert engine._last_fast_monitor_exit_reason == "rate-limited"
    messages = [message for message, _level in logs]
    assert any("요청 제한(HTTP 429) 감지" in message for message in messages)
    assert any("이미 열린 브라우저 좌석 화면" in message for message in messages)
    assert not any("3.0초 후 재시도" in message for message in messages)
    assert not any("6.0초 후 재시도" in message for message in messages)


def test_http_403_is_reported_as_access_denied_not_rate_limit(monkeypatch):
    logs = []
    engine = _make_engine(logs)

    monkeypatch.setattr(
        BaseCgvEngine,
        "_read_fast_seat_monitor",
        staticmethod(
            lambda _page: {
                "running": False,
                "completed": 1,
                "lastStatus": 403,
                "blocked": True,
                "failureKind": "forbidden",
                "terminalError": "",
                "hit": None,
            }
        ),
    )

    held, fallback = engine._watch_and_hold_api(
        _Page(),
        {"siteNo": "0013", "scnYmd": "20260826"},
        (),
        2,
        False,
        {},
    )

    assert (held, fallback) == (False, True)
    assert engine._last_fast_monitor_exit_reason == "access-forbidden"
    messages = [message for message, _level in logs]
    assert any("접근 거부(HTTP 403)" in message for message in messages)
    assert not any("요청 제한(HTTP 429)" in message for message in messages)


def test_consecutive_fetch_errors_fall_back_instead_of_restarting_monitor(monkeypatch):
    logs = []
    engine = _make_engine(logs)

    monkeypatch.setattr(
        BaseCgvEngine,
        "_read_fast_seat_monitor",
        staticmethod(
            lambda _page: {
                "running": False,
                "attempts": 5,
                "completed": 5,
                "inflight": 0,
                "consecutiveErrors": engine.FAST_MONITOR_MAX_CONSECUTIVE_ERRORS,
                "lastStatus": 0,
                "blocked": False,
                "unauthorized": False,
                "lastError": "TypeError: Failed to fetch",
                "terminalError": "",
                "conflicts": 0,
                "hit": None,
            }
        ),
    )

    held, fallback = engine._watch_and_hold_api(
        _Page(),
        {"siteNo": "0013", "scnYmd": "20260826"},
        (),
        2,
        False,
        {},
    )

    assert (held, fallback) == (False, True)
    assert engine._last_fast_monitor_exit_reason == "consecutive-fetch-errors"
    messages = [message for message, _level in logs]
    assert any("연속 조회 실패" in message for message in messages)
    assert any("이미 열린 브라우저 좌석 화면" in message for message in messages)


def test_claim_in_progress_is_not_misclassified_as_stopped_fetch_errors(monkeypatch):
    logs = []
    engine = _make_engine(logs)
    monkeypatch.setattr(
        BaseCgvEngine,
        "_read_fast_seat_monitor",
        staticmethod(
            lambda _page: {
                "running": False,
                "claiming": True,
                "phase": "pricing",
                "consecutiveErrors": engine.FAST_MONITOR_MAX_CONSECUTIVE_ERRORS,
                "terminalError": "",
                "hit": None,
            }
        ),
    )

    snapshot = engine._read_fast_seat_monitor(_Page())

    assert snapshot["claiming"] is True
    assert snapshot.get("terminalError", "") == ""
    assert engine._fast_monitor_fallback_reason == ""


def test_missing_monitor_state_uses_safe_fallback(monkeypatch):
    logs = []
    engine = _make_engine(logs)

    monkeypatch.setattr(
        BaseCgvEngine,
        "_read_fast_seat_monitor",
        staticmethod(lambda _page: {}),
    )

    held, fallback = engine._watch_and_hold_api(
        _Page(),
        {"siteNo": "0013", "scnYmd": "20260826"},
        (),
        2,
        False,
        {},
    )

    assert (held, fallback) == (False, True)
    assert engine._last_fast_monitor_exit_reason == "monitor-state-lost"
    messages = [message for message, _level in logs]
    assert any("감시 상태를 읽지 못해" in message for message in messages)


def test_browser_fallback_keeps_existing_seat_dom_without_reload():
    logs = []
    engine = CgvEngine(lambda message, level="info": logs.append((message, level)))
    page = _Page()
    engine._seat_modal_snapshot = lambda _page: {
        "modalOpen": True,
        "seatCount": 336,
    }
    engine._reload_or_recover_seat_page = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("a ready seat DOM must not be reloaded")
    )

    returned_page, ready = engine._prepare_browser_fallback_page(
        page,
        schedule={"siteNo": "0013"},
        people=1,
        fallback_reason="rate-limited",
    )

    assert returned_page is page
    assert ready is True
    assert any("좌석 화면을 유지" in message for message, _level in logs)
