import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from engines.cgv_browser_client import CgvBrowserClient
from engines.cgv_client import CgvError


FIXTURES = Path(__file__).parent / "fixtures"


class _Context:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self, _url):
        return self._cookies


class _LoginContext:
    def __init__(self):
        self.reads = 0

    def cookies(self, _url):
        self.reads += 1
        if self.reads >= 3:
            return [{"name": "refresh_token", "value": "latest-login"}]
        return []


class _LoginPage:
    def __init__(self):
        self.context = _LoginContext()
        self.url = "https://cgv.co.kr/mem/login?nmbrAtktFlag=Y"
        self.waits = 0

    def is_closed(self):
        return False

    def wait_for_timeout(self, _milliseconds):
        self.waits += 1


class _RetryPage:
    def __init__(self, *, target_wins=False):
        self.url = "https://cgv.co.kr/mem/login"
        self.target_wins = target_wins
        self.goto_calls = 0
        self.waits = 0
        self.load_waits = 0

    def goto(self, url, **_kwargs):
        self.goto_calls += 1
        if self.goto_calls == 1:
            self.url = url if self.target_wins else "https://cgv.co.kr/mem/login"
            raise RuntimeError("page.goto: net::ERR_ABORTED")
        self.url = url
        return "ok"

    def wait_for_timeout(self, _milliseconds):
        self.waits += 1

    def wait_for_load_state(self, _state, **_kwargs):
        self.load_waits += 1


class _FreshLoginPage:
    def __init__(self):
        self.context = _Context([{"name": "accessToken", "value": "stale-or-new"}])
        self.url = "https://cgv.co.kr/mem/login?nmbrAtktFlag=Y"
        self.waits = 0

    def is_closed(self):
        return False

    def wait_for_timeout(self, _milliseconds):
        self.waits += 1
        self.url = "https://cgv.co.kr/"


def test_member_session_requires_nonempty_cgv_auth_cookie():
    assert CgvBrowserClient._has_member_session(
        _Context([{"name": "accessToken", "value": "member-token"}])
    ) is True
    assert CgvBrowserClient._has_member_session(
        _Context([{"name": "accessToken", "value": ""}])
    ) is False
    assert CgvBrowserClient._has_member_session(
        _Context([{"name": "unrelated", "value": "value"}])
    ) is False


def test_login_wait_keeps_page_alive_until_latest_session_appears():
    events = []
    page = _LoginPage()
    client = CgvBrowserClient(log=lambda message, level: events.append((message, level)))
    client._wait_for_post_login_navigation = lambda _page: None

    client._wait_for_member_login(page, timeout_seconds=1)

    assert page.waits == 1
    assert any(level == "success" for _message, level in events)


def test_login_required_page_does_not_trust_a_stale_cookie_alone():
    page = _FreshLoginPage()
    client = CgvBrowserClient()
    client._wait_for_post_login_navigation = lambda _page: None

    client._wait_for_member_login(page, timeout_seconds=1, require_fresh_login=True)

    assert page.waits == 1
    assert "/mem/login" not in page.url


def test_aborted_login_redirect_retries_target_navigation():
    events = []
    page = _RetryPage()
    client = CgvBrowserClient(log=lambda message, level: events.append((message, level)))

    result = client._goto_with_retry(page, "https://cgv.co.kr/cnm/movieBook/cinema")

    assert result == "ok"
    assert page.goto_calls == 2
    assert page.waits == 1
    assert any(level == "warning" for _message, level in events)


def test_aborted_navigation_is_accepted_when_target_route_won():
    page = _RetryPage(target_wins=True)
    client = CgvBrowserClient()

    result = client._goto_with_retry(page, "https://cgv.co.kr/cnm/movieBook/cinema")

    assert result is None
    assert page.goto_calls == 1
    assert page.load_waits == 1


class _ScheduleClient(CgvBrowserClient):
    def __init__(self, schedules):
        super().__init__()
        self.schedules = schedules
        self.requested_dates = []

    def _with_page(self, operation):
        return operation(object())

    def _fetch_schedule_on_page(self, _page, _site_no, date_digits):
        self.requested_dates.append(date_digits)
        return tuple(self.schedules.get(date_digits, ()))


def test_unopened_target_uses_nearest_published_schedule_as_waiting_template():
    target = datetime.now().date() + timedelta(days=5)
    reference = target - timedelta(days=2)
    reference_schedule = {
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "scnsrtTm": "2130",
        "scnYmd": reference.strftime("%Y%m%d"),
    }
    client = _ScheduleClient({reference.strftime("%Y%m%d"): (reference_schedule,)})

    schedules, reference_date, reference_only = client.fetch_schedule_with_reference(
        "0013", target.isoformat()
    )

    assert len(schedules) == 1
    assert schedules[0]["expoProdNm"] == "오디세이"
    assert schedules[0]["scnsrtTm"] == "2130"
    assert schedules[0]["_pengucroPreopen"] is True
    assert schedules[0]["_pengucroSeatReference"] == reference_schedule
    assert reference_date == reference.isoformat()
    assert reference_only is True


def test_multi_day_candidate_discovery_aggregates_across_dates_without_dropping_movies():
    target = datetime.now().date() + timedelta(days=5)
    day_minus_1 = target - timedelta(days=1)
    day_minus_2 = target - timedelta(days=2)
    day_minus_3 = target - timedelta(days=3)

    sched_movie_a = {
        "expoProdNm": "영화 A",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "scnsrtTm": "1900",
        "scnYmd": day_minus_1.strftime("%Y%m%d"),
    }
    sched_odyssey_1 = {
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "scnsrtTm": "1400",
        "scnYmd": day_minus_2.strftime("%Y%m%d"),
    }
    sched_odyssey_2 = {
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "scnsrtTm": "1730",
        "scnYmd": day_minus_3.strftime("%Y%m%d"),
    }

    client = _ScheduleClient(
        {
            day_minus_1.strftime("%Y%m%d"): (sched_movie_a,),
            day_minus_2.strftime("%Y%m%d"): (sched_odyssey_1,),
            day_minus_3.strftime("%Y%m%d"): (sched_odyssey_2,),
        }
    )

    schedules, reference_date, reference_only = client.fetch_schedule_with_reference(
        "0013", target.isoformat()
    )

    assert reference_only is True
    movie_names = {s["expoProdNm"] for s in schedules}
    assert "영화 A" in movie_names
    assert "오디세이" in movie_names

    odyssey_times = {s["scnsrtTm"] for s in schedules if s["expoProdNm"] == "오디세이"}
    assert odyssey_times == {"1400", "1730"}


def test_real_target_date_schedule_takes_priority_over_historical_templates():
    target = datetime.now().date() + timedelta(days=4)
    reference = target - timedelta(days=1)

    target_real_odyssey = {
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "scnYmd": target.strftime("%Y%m%d"),
        "scnsrtTm": "2000",
        "frSeatCnt": 50,
    }
    historical_odyssey = {
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "scnYmd": reference.strftime("%Y%m%d"),
        "scnsrtTm": "1400",
        "frSeatCnt": 100,
    }

    client = _ScheduleClient(
        {
            target.strftime("%Y%m%d"): (target_real_odyssey,),
            reference.strftime("%Y%m%d"): (historical_odyssey,),
        }
    )

    schedules, reference_date, reference_only = client.fetch_schedule_with_reference(
        "0013", target.isoformat()
    )

    assert reference_only is False
    assert len(schedules) == 1
    assert schedules[0]["expoProdNm"] == "오디세이"
    assert schedules[0]["scnsrtTm"] == "2000"
    assert not schedules[0].get("_pengucroPreopen")


def test_prepublished_target_attaches_recent_real_seat_layout_reference():
    target = datetime.now().date() + timedelta(days=4)
    reference = target - timedelta(days=1)
    target_schedule = {
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "scnYmd": target.strftime("%Y%m%d"),
        "scnsrtTm": "2130",
        "frSeatCnt": 0,
    }
    reference_schedule = {
        "expoProdNm": "다른 영화",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "scnYmd": reference.strftime("%Y%m%d"),
        "scnsrtTm": "1930",
        "frSeatCnt": 120,
    }
    client = _ScheduleClient(
        {
            target.strftime("%Y%m%d"): (target_schedule,),
            reference.strftime("%Y%m%d"): (reference_schedule,),
        }
    )

    schedules, reference_date, reference_only = client.fetch_schedule_with_reference(
        "0013", target.isoformat()
    )

    assert reference_date == target.isoformat()
    assert reference_only is False
    assert schedules[0]["scnYmd"] == target.strftime("%Y%m%d")
    assert schedules[0]["_pengucroSeatReference"] == reference_schedule
    assert schedules[0]["_pengucroSeatReferenceDate"] == reference.isoformat()


def test_booking_dialog_uses_recent_layout_when_target_date_button_is_not_open():
    from ui.cgv_booking_dialog import CgvBookingDialog

    reference = {"scnYmd": "20260825", "frSeatCnt": 100}
    unavailable = {
        "scnYmd": "20260826",
        "frSeatCnt": 0,
        "_pengucroSeatReference": reference,
    }
    assert CgvBookingDialog._seat_reference_schedule(
        SimpleNamespace(selected_schedule=unavailable, schedules=(unavailable,))
    ) == reference
    assert CgvBookingDialog._seat_reference_schedule(
        SimpleNamespace(
            selected_schedule=dict(unavailable, frSeatCnt=20),
            schedules=(unavailable,),
        )
    ) == reference


def test_target_odyssey_2d_does_not_suppress_historical_odyssey_imax():
    target = datetime.now().date() + timedelta(days=4)
    reference = target - timedelta(days=1)

    target_2d_odyssey = {
        "expoProdNm": "오디세이",
        "expoScnsNm": "2관",
        "movkndDsplEnm": "2D 일반",
        "scnYmd": target.strftime("%Y%m%d"),
        "scnsrtTm": "1900",
        "frSeatCnt": 50,
    }
    historical_imax_odyssey = {
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "scnYmd": reference.strftime("%Y%m%d"),
        "scnsrtTm": "1400",
        "frSeatCnt": 100,
    }

    client = _ScheduleClient(
        {
            target.strftime("%Y%m%d"): (target_2d_odyssey,),
            reference.strftime("%Y%m%d"): (historical_imax_odyssey,),
        }
    )

    schedules, reference_date, reference_only = client.fetch_schedule_with_reference(
        "0013", target.isoformat()
    )

    # Both target 2D and historical IMAX candidate should be available (IMAX candidate not suppressed!)
    assert any(s["expoProdNm"] == "오디세이" and "IMAX" in s.get("expoScnsNm", "") for s in schedules)


def test_historical_ordinary_2d_candidate_excluded_and_imax_included():
    target = datetime.now().date() + timedelta(days=4)
    reference = target - timedelta(days=1)

    sched_2d = {
        "expoProdNm": "일반영화",
        "expoScnsNm": "1관",
        "movkndDsplEnm": "2D 일반",
        "scnYmd": reference.strftime("%Y%m%d"),
        "scnsrtTm": "1500",
    }
    sched_imax = {
        "expoProdNm": "아이맥스영화",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "scnYmd": reference.strftime("%Y%m%d"),
        "scnsrtTm": "1730",
    }

    client = _ScheduleClient(
        {
            reference.strftime("%Y%m%d"): (sched_2d, sched_imax),
        }
    )

    schedules, _, reference_only = client.fetch_schedule_with_reference(
        "0013", target.isoformat()
    )

    movie_names = {s["expoProdNm"] for s in schedules}
    assert "아이맥스영화" in movie_names
    assert "일반영화" not in movie_names


def test_empty_imax_site_catalog_raises_error_without_silent_fallback():
    import pytest
    from engines.cgv_client import CgvError
    from engines.cgv_browser_client import CgvBrowserClient

    client = CgvBrowserClient()
    # Payload with regions and non-IMAX sites only (0 IMAX sites)
    fake_payload = {
        "statusCode": 0,
        "data": {
            "regionList": [{"regionCd": "01", "regionNm": "서울", "siteCnt": 1}],
            "siteList": [{"siteNo": "9999", "siteNm": "일반영화관", "regionCd": "01"}],
        },
    }
    client._with_page = lambda op: op(None)
    client._fetch_json = lambda page, path: fake_payload

    with pytest.raises(CgvError, match="CGV IMAX 지점 정보를 확인하지 못했습니다"):
        client.fetch_catalog(imax_only=True)


def test_fetch_catalog_filters_with_official_dynamic_imax_sites():
    events = []
    client = CgvBrowserClient(log=lambda message, level: events.append((message, level)))
    catalog = {
        "statusCode": 0,
        "data": {
            "regionInfo": [
                {"comCdval": "02", "comCdvalNm": "경기", "cnt": "2"},
                {"comCdval": "05", "comCdvalNm": "부산", "cnt": "1"},
                {"comCdval": "03", "comCdvalNm": "대전", "cnt": "1"},
            ],
            "siteInfo": [
                {"regnGrpCd": "02", "siteNo": "0257", "siteNm": "광교"},
                {"regnGrpCd": "05", "siteNo": "0089", "siteNm": "센텀시티"},
                {"regnGrpCd": "03", "siteNo": "0286", "siteNm": "대전가수원"},
            ],
        },
    }
    components = json.loads(
        (FIXTURES / "cgv_imax_display_components.json").read_text(encoding="utf-8")
    )
    detail = json.loads(
        (FIXTURES / "cgv_imax_site_detail.json").read_text(encoding="utf-8")
    )
    requested_paths = []

    def fetch_json(_page, path):
        requested_paths.append(path)
        if "searchAllRegionAndSite" in path:
            return catalog
        if "searchSscnsDspCpotList" in path:
            return components
        if "searchScrDspCpotDtl" in path:
            return detail
        raise AssertionError(path)

    client._with_page = lambda operation: operation(object())
    client._fetch_json = fetch_json

    snapshot = client.fetch_catalog(imax_only=True)

    assert [site.site_no for site in snapshot.sites] == ["0257", "0089"]
    assert [(region.code, region.count) for region in snapshot.regions] == [
        ("02", 1),
        ("05", 1),
    ]
    assert any(
        path.startswith(
            "/api/v1/common/meta/dsp/sscnsDsp/searchSscnsDspCpotList?"
        )
        and "sscnsNo=1" in path
        for path in requested_paths
    )
    assert any(
        path.startswith(
            "/api/v1/common/meta/dsp/scrDsp/searchScrDspCpotDtl?"
        )
        and "unitCpotRelNo=64" in path
        for path in requested_paths
    )
    assert any("공식 IMAX 지점 목록 적용 · 25개" in message for message, _ in events)


def test_fetch_catalog_uses_corrected_static_imax_fallback_on_dynamic_failure():
    events = []
    client = CgvBrowserClient(log=lambda message, level: events.append((message, level)))
    catalog = {
        "statusCode": 0,
        "data": {
            "regionInfo": [
                {"comCdval": "02", "comCdvalNm": "경기", "cnt": "2"}
            ],
            "siteInfo": [
                {"regnGrpCd": "02", "siteNo": "0257", "siteNm": "광교"},
                {"regnGrpCd": "02", "siteNo": "0286", "siteNm": "대전가수원"},
            ],
        },
    }

    def fetch_json(_page, path):
        if "searchAllRegionAndSite" in path:
            return catalog
        raise CgvError("temporary IMAX metadata failure")

    client._with_page = lambda operation: operation(object())
    client._fetch_json = fetch_json

    snapshot = client.fetch_catalog(imax_only=True)

    assert [site.site_no for site in snapshot.sites] == ["0257"]
    assert any(
        "내장 목록으로 계속합니다" in message and level == "warning"
        for message, level in events
    )


def test_fetch_catalog_does_not_request_imax_metadata_for_full_catalog():
    client = CgvBrowserClient()
    catalog = {
        "statusCode": 0,
        "data": {
            "regionInfo": [
                {"comCdval": "02", "comCdvalNm": "경기", "cnt": "1"}
            ],
            "siteInfo": [
                {"regnGrpCd": "02", "siteNo": "0286", "siteNm": "대전가수원"}
            ],
        },
    }
    requested_paths = []

    def fetch_json(_page, path):
        requested_paths.append(path)
        return catalog

    client._with_page = lambda operation: operation(object())
    client._fetch_json = fetch_json

    snapshot = client.fetch_catalog(imax_only=False)

    assert [site.site_no for site in snapshot.sites] == ["0286"]
    assert len(requested_paths) == 1
    assert "searchAllRegionAndSite" in requested_paths[0]


def test_fetch_json_timeout_raises_cgv_error():
    import pytest
    from engines.cgv_client import CgvError

    class _MockPage:
        def evaluate(self, js, args):
            # Simulate AbortError timeout
            return {"status": 408, "timeout": True, "error": "TIMEOUT"}

    page = _MockPage()
    with pytest.raises(CgvError, match="CGV 데이터 조회 응답 시간이 초과되었습니다"):
        CgvBrowserClient._fetch_json(page, "/api/v1/booking/searchMovScnInfo")


def test_with_page_retries_once_on_recoverable_browser_disconnect():
    import pytest
    from unittest.mock import MagicMock, patch
    from engines.cgv_client import CgvError

    events = []
    client = CgvBrowserClient(log=lambda msg, level: events.append((msg, level)))

    attempts = 0
    def mock_op(page):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("TargetClosedError: Target page, context or browser has been closed")
        return "recovered_result"

    # Mock playwright & browser_session
    mock_chrome = MagicMock()
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_browser.contexts = [mock_context]
    mock_context.new_page.return_value = mock_page

    with patch("engines.browser_session.start_isolated", return_value=mock_chrome), \
         patch("playwright.sync_api.sync_playwright") as mock_pw_ctx:
        mock_pw = MagicMock()
        mock_pw.chromium.connect_over_cdp.return_value = mock_browser
        mock_pw_ctx.return_value.__enter__.return_value = mock_pw

        result = client._with_page(mock_op)
        assert result == "recovered_result"
        assert attempts == 2
        assert any("1회 자동 복구" in msg for msg, lvl in events)
        assert any("자동 복구 성공" in msg for msg, lvl in events)
        assert mock_page.close.call_count == 2
        assert mock_chrome.release.call_count == 2


def test_with_page_fails_on_second_recovery_without_infinite_loop():
    import pytest
    from unittest.mock import MagicMock, patch

    events = []
    client = CgvBrowserClient(log=lambda msg, level: events.append((msg, level)))

    attempts = 0
    def mock_op(page):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("TargetClosedError: Target page has been closed")

    mock_chrome = MagicMock()
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_browser.contexts = [mock_context]
    mock_context.new_page.return_value = mock_page

    with patch("engines.browser_session.start_isolated", return_value=mock_chrome), \
         patch("playwright.sync_api.sync_playwright") as mock_pw_ctx:
        mock_pw = MagicMock()
        mock_pw.chromium.connect_over_cdp.return_value = mock_browser
        mock_pw_ctx.return_value.__enter__.return_value = mock_pw

        with pytest.raises(RuntimeError, match="TargetClosedError"):
            client._with_page(mock_op)
        assert attempts == 2  # exactly 2 attempts, no infinite loop
        assert any("자동 복구 실패" in msg for msg, lvl in events)


def test_fetch_schedule_with_reference_raises_cgv_request_cancelled_when_event_set():
    import threading
    import pytest
    from engines.cgv_browser_client import CgvBrowserClient, CgvRequestCancelled

    client = CgvBrowserClient()
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(CgvRequestCancelled):
        client.fetch_schedule_with_reference("0013", "2026-08-26", cancel_event=cancel_event)


def test_fetch_catalog_raises_cgv_request_cancelled_when_event_set():
    import threading
    import pytest
    from engines.cgv_browser_client import CgvBrowserClient, CgvRequestCancelled

    client = CgvBrowserClient()
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(CgvRequestCancelled):
        client.fetch_catalog(imax_only=True, cancel_event=cancel_event)


def test_fetch_seat_map_raises_cgv_request_cancelled_when_event_set():
    import threading
    import pytest
    from engines.cgv_browser_client import CgvBrowserClient, CgvRequestCancelled

    client = CgvBrowserClient()
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(CgvRequestCancelled):
        client.fetch_seat_map({"siteNo": "0013"}, 2, cancel_event=cancel_event)


