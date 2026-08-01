"""Tests for the Naver engine's decision logic (no browser, no network)."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from engines.naver_api import NaverSlot, SubmitOutcome, SubmitResult
from engines.naver_engine import NaverEngine
from engines.naver_submit import NaverSubmitPreparation

KST = timezone(timedelta(hours=9))


def make_engine():
    return NaverEngine(lambda *_args, **_kwargs: None)


def make_slot(**overrides):
    payload = {
        "slotId": "1", "scheduleId": "2", "detailScheduleId": None,
        "id": "composite", "unitStartTime": "2026-07-31 13:20:00",
        "unitStartDateTime": None, "stock": 1, "bookingCount": 0,
        "occupiedBookingCount": 0, "unitStock": 1, "unitBookingCount": 0,
        "isBusinessDay": True, "isSaleDay": True, "isUnitSaleDay": True,
        "isUnitBusinessDay": True, "isHoliday": False,
        "minBookingCount": 1, "maxBookingCount": 1,
        "saleStartDateTime": None, "saleEndDateTime": None,
    }
    payload.update(overrides)
    return NaverSlot.from_payload(payload)


class FakeClock:
    """Reports a fixed number of seconds until whatever it is asked about."""

    def __init__(self, remaining):
        self.remaining = remaining

    def seconds_until(self, _target):
        return self.remaining

    def now_kst(self):
        return datetime.now(KST)

    def sync(self, _announce=False):
        return True


class CountdownClock(FakeClock):
    """Walks toward the open moment every time it is asked, so loops terminate."""

    def __init__(self, remaining, step=1.0):
        super().__init__(remaining)
        self.step = step
        self.queries = 0

    def seconds_until(self, _target):
        self.queries += 1
        current = self.remaining
        self.remaining -= self.step
        return current


class FakeApi:
    """find_slot only: the loop needs nothing else."""

    def __init__(self, slot):
        self.slot = slot
        self.calls = 0

    def find_slot(self, _date, _time):
        self.calls += 1
        return self.slot


class FakePreparationApi:
    def fetch_business(self):
        return {
            "businessId": "1498729",
            "businessTypeId": 12,
            "rawNames": {"name": "제로월드", "serviceName": "방탈출 예약"},
            "bookingConfirmCode": "CF02",
            "refundPolicy": {"refundPolicyId": 17},
            "agencies": [],
            "businessResources": [],
            "customFormJson": [],
        }

    def fetch_biz_item_raw(self):
        return {
            "bizItemId": "7094790",
            "name": "사요나라, 세이코!",
            "isNPayUsed": False,
            "isPeriodFixed": False,
            "isSeatUsed": False,
            "bookingConfirmCode": "CF01",
            "resources": [],
        }

    def fetch_slot_raw(self, _date, _time):
        return {
            "slotId": "1331382668",
            "name": "",
            "unitStartDateTime": "2026-08-08T05:30:00Z",
            "minBookingCount": 1,
            "maxBookingCount": 1,
            "prices": [{"priceId": "1", "price": 33000, "isImp": True}],
        }


class DelayedSlotPreparationApi(FakePreparationApi):
    def __init__(self):
        self.business_calls = 0
        self.item_calls = 0
        self.slot_calls = 0

    def fetch_business(self):
        self.business_calls += 1
        return super().fetch_business()

    def fetch_biz_item_raw(self):
        self.item_calls += 1
        return super().fetch_biz_item_raw()

    def fetch_slot_raw(self, date, time_str):
        self.slot_calls += 1
        if self.slot_calls == 1:
            return None
        return super().fetch_slot_raw(date, time_str)


class AccountPage:
    async def evaluate(self, _script, argument=None):
        assert argument["operationName"] == "account"
        return {"status": 200, "body": {"data": {"account": {
            "isLoggedIn": True,
            "csrfToken": "csrf-secret",
            "isSmsAlarm": False,
        }}}}


PREPARATION_RESERVATION = {
    "reservationDate": "2026-08-08",
    "reservationTime": "14:30",
    "name": "홍길동",
    "phone": "010-1234-5678",
    "people": "3",
}


class FakeSubmitter:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def submit(self, _payload):
        self.calls += 1
        return self.results.pop(0)


def arm_direct_submit(engine, results):
    engine._api_submitter = FakeSubmitter(results)
    engine._api_preparation = NaverSubmitPreparation(
        True,
        payload={"slotId": "1331382668", "csrfToken": "secret"},
        slot_id="1331382668",
    )
    engine._api_submit_enabled = True


def test_prepare_api_submit_arms_direct_path():
    logs = []
    engine = NaverEngine(lambda message, _level: logs.append(message))
    engine.api = FakePreparationApi()
    engine._page = AccountPage()

    asyncio.run(
        engine._prepare_api_submit(PREPARATION_RESERVATION, dev_mode=False)
    )

    assert engine._api_submit_enabled is True
    assert engine._api_preparation.slot_id == "1331382668"
    assert "API 직접 제출 준비 완료" in "\n".join(logs)


def test_developer_mode_prepares_but_never_arms_transport():
    logs = []
    engine = NaverEngine(lambda message, _level: logs.append(message))
    engine.api = FakePreparationApi()
    engine._page = AccountPage()

    asyncio.run(
        engine._prepare_api_submit(PREPARATION_RESERVATION, dev_mode=True)
    )

    assert engine._api_submit_enabled is False
    assert engine._api_preparation.ready is True
    joined = "\n".join(logs)
    assert "개발자 테스트" in joined
    assert "필드 60개" in joined
    assert "csrfToken" in joined
    assert "csrf-secret" not in joined
    assert "01012345678" not in joined
    assert "홍길동" not in joined


def test_api_prepare_does_not_rearm_after_direct_path_was_blocked():
    engine = NaverEngine(lambda *_args: None)
    engine.api = FakePreparationApi()
    engine._page = AccountPage()
    engine._api_submit_blocked = True

    asyncio.run(
        engine._prepare_api_submit(PREPARATION_RESERVATION, dev_mode=False)
    )

    assert engine._api_submit_enabled is False
    assert engine._api_preparation is None


def test_api_prepare_reuses_static_data_when_delayed_slot_appears():
    engine = NaverEngine(lambda *_args: None)
    engine.api = DelayedSlotPreparationApi()
    engine._page = AccountPage()

    asyncio.run(
        engine._prepare_api_submit(PREPARATION_RESERVATION, dev_mode=False)
    )
    assert engine._api_prepare_pending is True
    assert engine._api_submit_enabled is False

    asyncio.run(
        engine._prepare_api_submit(PREPARATION_RESERVATION, dev_mode=False)
    )

    assert engine._api_prepare_pending is False
    assert engine._api_submit_enabled is True
    assert engine.api.business_calls == 1
    assert engine.api.item_calls == 1
    assert engine.api.slot_calls == 2


# -- the opening moment ----------------------------------------------------
def test_open_strike_is_claimed_once_inside_the_arming_window():
    engine = make_engine()
    engine._open_at_epoch = 123.0
    engine._open_strike_pending = True
    engine._page = object()

    assert engine._claim_open_strike(1.0) is True
    assert engine._claim_open_strike(1.0) is False


def test_open_strike_arms_five_seconds_early_for_final_clock_sync():
    engine = make_engine()
    engine._open_strike_pending = True
    engine._page = object()

    assert engine._claim_open_strike(4.5) is True


def test_open_strike_is_not_claimed_while_the_boundary_is_far_off():
    engine = make_engine()
    engine._open_at_epoch = 123.0
    engine._open_strike_pending = True
    engine._page = object()

    assert engine._claim_open_strike(30.0) is False
    assert engine._claim_open_strike(None) is False
    assert engine._open_strike_pending is True


def test_seconds_until_open_is_none_without_a_published_open_time():
    engine = make_engine()
    assert engine._seconds_until_open() is None
    engine._open_at_epoch = 5.0
    engine.clock = FakeClock(12.5)
    assert engine._seconds_until_open() == 12.5


def test_wait_for_open_returns_at_the_boundary():
    engine = make_engine()
    engine._open_at_epoch = 1.0
    engine.clock = CountdownClock(0.05, step=0.02)
    started = time.monotonic()
    asyncio.run(engine._wait_for_open())
    assert engine.clock.remaining <= 0
    assert time.monotonic() - started < 1.0


def test_strike_reloads_at_open_and_submits_on_the_same_turn():
    """The reload and the submit belong to one turn: no API round trip between."""
    engine = make_engine()
    engine._open_at_epoch = 1.0
    engine.clock = FakeClock(-0.01)
    calls = []

    async def fake_goto(date, timeout_ms=None):
        calls.append(("goto", date, timeout_ms))
        return True

    async def fake_submit(date, time_str, _data, _dev):
        calls.append(("submit", date, time_str))
        return "success", "ok"

    engine._goto_item = fake_goto
    engine._submit = fake_submit

    outcome, _detail = asyncio.run(
        engine._strike_at_open("2026-08-06", "19:10", {}, False)
    )
    assert outcome == "success"
    assert [entry[0] for entry in calls] == ["goto", "submit"]
    assert engine._warmed_for_date is True


def test_api_ready_strike_submits_without_page_reload():
    engine = make_engine()
    engine._open_at_epoch = 1.0
    engine.clock = FakeClock(-0.01)
    arm_direct_submit(engine, [
        SubmitResult(SubmitOutcome.SUCCESS, booking_id="999888"),
    ])

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("API-ready strike must not drive the page")

    engine._goto_item = forbidden
    engine._submit = forbidden

    outcome, detail = asyncio.run(
        engine._strike_at_open(
            "2026-08-08", "14:30", PREPARATION_RESERVATION, False
        )
    )

    assert outcome == "success"
    assert "999888" in detail
    assert engine._api_submitter.calls == 1


def test_pending_api_strike_refreshes_slot_before_page_reload():
    engine = make_engine()
    engine._open_at_epoch = 1.0
    engine.clock = FakeClock(-0.01)
    engine._api_prepare_pending = True
    prepare_calls = {"count": 0}

    async def fake_prepare(_reservation, dev_mode):
        assert dev_mode is False
        prepare_calls["count"] += 1
        arm_direct_submit(engine, [
            SubmitResult(SubmitOutcome.SUCCESS, booking_id="999888"),
        ])
        engine._api_prepare_pending = False

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("refreshed strike must not drive the page")

    engine._prepare_api_submit = fake_prepare
    engine._goto_item = forbidden
    engine._submit = forbidden

    outcome, detail = asyncio.run(
        engine._strike_at_open(
            "2026-08-08", "14:30", PREPARATION_RESERVATION, False
        )
    )

    assert outcome == "success"
    assert "999888" in detail
    assert prepare_calls["count"] == 1


def test_api_not_open_retries_until_success_without_reloading():
    engine = make_engine()
    engine._open_at_epoch = 1.0
    engine.clock = FakeClock(-0.01)
    arm_direct_submit(engine, [
        SubmitResult(SubmitOutcome.NOT_OPEN, message="아직 오픈 전"),
        SubmitResult(SubmitOutcome.SUCCESS, booking_id="999888"),
    ])

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("NOT_OPEN retry must stay on the API path")

    engine._goto_item = forbidden
    engine._submit = forbidden

    outcome, _detail = asyncio.run(
        engine._strike_at_open(
            "2026-08-08", "14:30", PREPARATION_RESERVATION, False
        )
    )

    assert outcome == "success"
    assert engine._api_submitter.calls == 2


def test_api_rt98_disables_direct_path_and_falls_back_to_browser():
    engine = make_engine()
    engine._open_at_epoch = 1.0
    engine.clock = FakeClock(-0.01)
    arm_direct_submit(engine, [
        SubmitResult(
            SubmitOutcome.ABUSE,
            code="RT98",
            message="비정상 요청 탐지",
        ),
    ])
    calls = []

    async def fake_goto(date, timeout_ms=None):
        calls.append(("goto", date, timeout_ms))
        return True

    async def fake_submit(date, time_str, _data, _dev):
        calls.append(("submit", date, time_str))
        return "taken", "화면 매진"

    engine._goto_item = fake_goto
    engine._submit = fake_submit

    outcome, _detail = asyncio.run(
        engine._strike_at_open(
            "2026-08-08", "14:30", PREPARATION_RESERVATION, False
        )
    )

    assert outcome == "taken"
    assert engine._api_submit_enabled is False
    assert [entry[0] for entry in calls] == ["goto", "submit"]


def test_api_refusal_keeps_watching_without_browser_reload():
    engine = make_engine()
    engine._open_at_epoch = 1.0
    engine.clock = FakeClock(-0.01)
    arm_direct_submit(engine, [
        SubmitResult(
            SubmitOutcome.REFUSED,
            code="RT47",
            message="정원 마감",
        ),
    ])

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("server refusal is authoritative")

    engine._goto_item = forbidden
    engine._submit = forbidden

    outcome, detail = asyncio.run(
        engine._strike_at_open(
            "2026-08-08", "14:30", PREPARATION_RESERVATION, False
        )
    )

    assert outcome == "taken"
    assert "RT47" in detail
    assert engine._api_submit_enabled is True


def test_strike_retries_the_reload_and_reports_notready_without_submitting():
    engine = make_engine()
    engine._open_at_epoch = 1.0
    engine.clock = FakeClock(-0.01)
    engine.OPEN_RELOAD_ATTEMPTS = 3
    loads = {"n": 0}

    async def fake_goto(_date, timeout_ms=None):
        loads["n"] += 1
        return False

    async def fake_submit(*_args):
        raise AssertionError("submit must not run without a rendered timetable")

    engine._goto_item = fake_goto
    engine._submit = fake_submit

    outcome, detail = asyncio.run(
        engine._strike_at_open("2026-08-06", "19:10", {}, False)
    )
    assert outcome == "notready"
    assert loads["n"] == 3
    assert "렌더링되지 않았습니다" in detail


def test_strike_survives_a_reload_that_raises():
    engine = make_engine()
    engine._open_at_epoch = 1.0
    engine.clock = FakeClock(-0.01)
    engine.OPEN_RELOAD_ATTEMPTS = 2
    attempts = {"n": 0}

    async def fake_goto(_date, timeout_ms=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("net::ERR_ABORTED")
        return True

    async def fake_submit(*_args):
        return "success", "ok"

    engine._goto_item = fake_goto
    engine._submit = fake_submit

    outcome, _detail = asyncio.run(
        engine._strike_at_open("2026-08-06", "19:10", {}, False)
    )
    assert outcome == "success"
    assert attempts["n"] == 2


# -- pre-open gating -------------------------------------------------------
def run_loop(engine, date="2026-08-06", time_str="19:10"):
    asyncio.run(engine.make_reservation_async_task(
        {"reservationDate": date, "reservationTime": time_str}, 0
    ))


def test_loop_never_submits_before_the_open_moment():
    """The defect this pins down.

    The schedule API reports the slot free before the page can act on it, and an
    unopened date renders no timetable at all, so each of those submits burned a
    ~7 s page cycle and returned "notready". Two of them either side of midnight
    meant a 00:00:00 opening was first looked at 00:00:05, by which time the one
    seat was gone. Nothing may drive the page until the boundary.
    """
    engine = make_engine()
    engine._poll_base = 0.01
    engine._poll_burst = 0.01
    engine._open_at_epoch = 100.0
    engine._open_strike_pending = True
    engine.clock = CountdownClock(7.0, step=1.0)
    engine.api = FakeApi(make_slot())
    engine._page = object()
    calls = {"submit": 0, "strike": 0, "rewarm": 0}

    async def fake_submit(*_args):
        calls["submit"] += 1
        return "retry", "should not happen"

    async def fake_strike(*_args):
        calls["strike"] += 1
        return "success", "ok"

    async def fake_rewarm(*_args, **_kwargs):
        calls["rewarm"] += 1

    engine._submit = fake_submit
    engine._strike_at_open = fake_strike
    engine._rewarm_if_needed = fake_rewarm
    engine.notify_success = lambda *_args, **_kwargs: True

    run_loop(engine)
    assert calls["submit"] == 0
    assert calls["strike"] == 1
    assert engine.api.calls >= 1


def test_loop_gives_the_strike_claim_back_when_the_lock_is_busy():
    """The boundary turn is the one thing that must not be dropped."""

    class OneMissLock:
        def __init__(self):
            self.attempts = 0

        def acquire(self, blocking=False):
            self.attempts += 1
            return self.attempts > 1

        def release(self):
            pass

    engine = make_engine()
    engine._poll_base = 0.01
    engine._poll_burst = 0.01
    engine._open_at_epoch = 100.0
    engine._open_strike_pending = True
    engine.clock = FakeClock(-0.5)
    engine.api = FakeApi(make_slot())
    engine._page = object()
    engine.submission_lock = OneMissLock()
    calls = {"strike": 0}

    async def fake_strike(*_args):
        calls["strike"] += 1
        return "success", "ok"

    engine._strike_at_open = fake_strike
    engine.notify_success = lambda *_args, **_kwargs: True

    run_loop(engine)
    assert calls["strike"] == 1
    assert engine.submission_lock.attempts == 2


def test_loop_submits_normally_once_the_open_moment_has_passed():
    engine = make_engine()
    engine._poll_base = 0.01
    engine._poll_burst = 0.01
    engine._open_at_epoch = 100.0
    engine._open_strike_pending = False
    engine.clock = FakeClock(-30.0)
    engine.api = FakeApi(make_slot())
    engine._page = object()
    calls = {"submit": 0}

    async def fake_submit(date, time_str, _data, _dev):
        calls["submit"] += 1
        assert (date, time_str) == ("2026-08-06", "19:10")
        return "success", "ok"

    engine._submit = fake_submit
    engine.notify_success = lambda *_args, **_kwargs: True

    run_loop(engine)
    assert calls["submit"] == 1


def test_loop_uses_prepared_api_for_a_later_cancellation():
    engine = make_engine()
    engine._poll_base = 0.01
    engine._poll_burst = 0.01
    engine.clock = FakeClock(-30.0)
    engine.api = FakeApi(make_slot())
    engine._page = object()
    arm_direct_submit(engine, [
        SubmitResult(SubmitOutcome.SUCCESS, booking_id="999888"),
    ])

    async def forbidden(*_args):
        raise AssertionError("prepared cancellation must use direct submit")

    engine._submit = forbidden
    engine.notify_success = lambda *_args, **_kwargs: True

    run_loop(engine)

    assert engine._api_submitter.calls == 1


def test_loop_refreshes_api_preparation_when_slot_first_appears():
    engine = make_engine()
    engine._poll_base = 0.01
    engine._poll_burst = 0.01
    engine.clock = FakeClock(-30.0)
    engine.api = FakeApi(make_slot())
    engine._page = object()
    engine._api_prepare_pending = True
    prepare_calls = {"count": 0}

    async def fake_prepare(_reservation, dev_mode):
        assert dev_mode is False
        prepare_calls["count"] += 1
        arm_direct_submit(engine, [
            SubmitResult(SubmitOutcome.SUCCESS, booking_id="999888"),
        ])
        engine._api_prepare_pending = False

    async def forbidden(*_args):
        raise AssertionError("refreshed slot must use direct submit")

    engine._prepare_api_submit = fake_prepare
    engine._submit = forbidden
    engine.notify_success = lambda *_args, **_kwargs: True

    run_loop(engine)

    assert prepare_calls["count"] == 1
    assert engine._api_submitter.calls == 1


def test_loop_backs_off_the_page_after_notready():
    """A page with no timetable gives the same answer if asked again instantly."""
    engine = make_engine()
    engine._poll_base = 0.01
    engine._poll_burst = 0.01
    engine.NOTREADY_BACKOFF_SECONDS = 0.3
    engine.clock = FakeClock(-30.0)
    engine.api = FakeApi(make_slot())
    engine._page = object()
    stamps = []

    async def fake_submit(*_args):
        stamps.append(time.monotonic())
        if len(stamps) >= 2:
            engine.stop_event.set()
        return "notready", "시간표가 렌더링되지 않았습니다"

    engine._submit = fake_submit
    run_loop(engine)
    assert len(stamps) == 2
    assert stamps[1] - stamps[0] >= 0.3


def test_rewarm_is_skipped_in_the_blackout_window_before_open():
    engine = make_engine()
    engine._page = object()
    loads = {"n": 0}

    async def fake_goto(_date, timeout_ms=None):
        loads["n"] += 1
        return True

    async def present():
        return False

    engine._goto_item = fake_goto
    engine._timetable_present = present

    # Inside the window: a reload here cannot render an unopened date and would
    # push the strike's own reload past the moment that decides the run.
    asyncio.run(engine._rewarm_if_needed("2026-08-06", True, 5.0))
    assert loads["n"] == 0

    # Outside it, the transition reload still happens.
    engine._last_warm = time.monotonic() - engine.REWARM_MIN_GAP_SECONDS - 1
    asyncio.run(engine._rewarm_if_needed("2026-08-06", True, 600.0))
    assert loads["n"] == 1


# -- polling cadence -------------------------------------------------------
def test_missing_slot_uses_the_base_rate():
    engine = make_engine()
    delay, tier = engine._poll_delay(None, time.monotonic())
    assert tier == "기본"
    assert delay == engine._poll_base


def test_published_but_full_slot_bursts():
    """A cancellation can free it at any instant, so poll hard."""
    engine = make_engine()
    delay, tier = engine._poll_delay(make_slot(bookingCount=1), time.monotonic())
    assert tier == "집중"
    assert delay == engine._poll_burst


def test_imminent_open_moment_bursts():
    engine = make_engine()
    engine._open_at_epoch = 1.0
    engine.clock = FakeClock(30.0)
    _delay, tier = engine._poll_delay(None, time.monotonic())
    assert tier == "집중"


def test_distant_open_moment_does_not_burst():
    engine = make_engine()
    engine._open_at_epoch = 1.0
    engine.clock = FakeClock(6000.0)
    _delay, tier = engine._poll_delay(None, time.monotonic())
    assert tier == "기본"


def test_relax_beats_burst_after_a_long_quiet_stretch():
    """The ordering bug this pins down.

    A published-but-full slot always takes the burst branch. With the relax check
    below it, a 매진 slot was polled at the burst rate indefinitely -- about
    4 req/s sustained, which is hundreds of thousands of requests overnight.
    """
    engine = make_engine()
    engine._relax_after = 60.0
    stale = time.monotonic() - 3600.0

    delay, tier = engine._poll_delay(make_slot(bookingCount=1), stale)
    assert tier == "절전"
    assert delay >= engine.POLL_RELAXED_SECONDS

    # Any change at all pulls it straight back.
    delay, tier = engine._poll_delay(make_slot(bookingCount=1), time.monotonic())
    assert tier == "집중"


def test_relax_can_be_switched_off():
    engine = make_engine()
    engine._relax_after = 0.0
    _delay, tier = engine._poll_delay(None, time.monotonic() - 100000.0)
    assert tier == "기본"


def test_relax_never_makes_polling_faster_than_base():
    engine = make_engine()
    engine._relax_after = 1.0
    engine._poll_base = 2.5
    delay, _tier = engine._poll_delay(None, time.monotonic() - 10.0)
    assert delay == 2.5


# -- change detection ------------------------------------------------------
def test_missing_slot_signature_is_distinct_from_unset():
    engine = make_engine()
    assert engine._slot_signature(None, "reason") == ("missing",)
    assert engine._slot_signature(None, "reason") != None  # noqa: E711


def test_signature_tracks_remaining_and_reason():
    engine = make_engine()
    free = engine._slot_signature(make_slot(), None)
    full = engine._slot_signature(make_slot(bookingCount=1), "정원 마감")
    assert free != full
    assert engine._slot_signature(make_slot(), None) == free


# -- failure classification ------------------------------------------------
@pytest.mark.parametrize(
    "message,expected",
    [
        ("이미 예약된 시간입니다", "taken"),
        ("예약이 마감되었습니다", "taken"),
        ("정원이 초과되었습니다", "taken"),
        ("매진되었습니다", "taken"),
        ("로그인이 필요합니다", "login"),
        ("본인확인이 필요합니다", "login"),
        ("알 수 없는 오류", "retry"),
        ("", "retry"),
    ],
)
def test_classify(message, expected):
    assert NaverEngine._classify(message) == expected


# -- misc ------------------------------------------------------------------
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (5, "5초"),
        (65, "1분 5초"),
        (3700, "1시간 1분"),
        (90000, "1일 1시간 0분"),
        (-10, "0초"),
    ],
)
def test_format_remaining(seconds, expected):
    assert NaverEngine._format_remaining(seconds) == expected


def test_resolve_url_prefers_theme_pk():
    engine = make_engine()
    assert engine._resolve_url({
        "themePK": "https://booking.naver.com/booking/12/bizes/1/items/2",
        "site_url": "https://example.com",
    }).endswith("/items/2")
    assert engine._resolve_url({"site_url": "https://example.com"}) == ""


def test_timing_reports_each_phase():
    timing = NaverEngine._Timing()
    timing.mark("탐색")
    timing.mark("슬롯선택")
    summary = timing.summary()
    assert "총" in summary and "탐색" in summary and "슬롯선택" in summary


def test_poll_for_returns_as_soon_as_ready():
    """No fixed sleeps in the critical path: it must return on the first hit."""
    calls = {"n": 0}

    async def probe():
        calls["n"] += 1
        return "ready" if calls["n"] >= 2 else None

    started = time.monotonic()
    result = asyncio.run(NaverEngine._poll_for(probe, timeout=2.0, interval=0.01))
    assert result == "ready"
    assert calls["n"] == 2
    assert time.monotonic() - started < 0.5


def test_poll_for_gives_up_and_survives_exceptions():
    async def probe():
        raise RuntimeError("boom")

    assert asyncio.run(NaverEngine._poll_for(probe, timeout=0.1, interval=0.01)) is None
