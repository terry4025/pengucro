from datetime import datetime, timedelta
from types import SimpleNamespace

from engines.cgv_browser_client import CgvBrowserClient


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
    }
    client = _ScheduleClient({reference.strftime("%Y%m%d"): (reference_schedule,)})

    schedules, reference_date, reference_only = client.fetch_schedule_with_reference(
        "0013", target.isoformat()
    )

    assert schedules == (reference_schedule,)
    assert reference_date == reference.isoformat()
    assert reference_only is True


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
