"""Tests for the Naver engine's decision logic (no browser, no network)."""

import asyncio
import time

import pytest

from engines.naver_api import NaverSlot
from engines.naver_engine import NaverEngine


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
