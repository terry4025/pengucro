"""Offline timing regressions: no Naver requests or real clock assumptions."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from engines.naver_api import NaverApiError, NaverBookingApi, NaverServerClock


BASE_EPOCH = 1_800_000_000.0


class ClockReads:
    def __init__(self):
        self.now = 0.0
        self.wall_offset = 0.0
        self.pending = []
        self.calls = 0

    def add(self, rtt, shift=0.0, *, count=1, missing=False, error=False):
        self.pending.extend([(rtt, shift, missing, error)] * count)

    def fetch_item_meta(self):
        self.calls += 1
        rtt, shift, missing, error = self.pending.pop(0)
        self.now += rtt
        if error:
            raise NaverApiError("offline read failure")
        server_time = None if missing else datetime.fromtimestamp(
            BASE_EPOCH + self.now + shift, timezone.utc
        )
        return SimpleNamespace(server_time=server_time)


@pytest.fixture
def measured_clock(monkeypatch):
    import engines.naver_api as module

    reads = ClockReads()
    monkeypatch.setattr(module.time, "monotonic", lambda: reads.now)
    monkeypatch.setattr(module.time, "time", lambda: BASE_EPOCH + reads.now + reads.wall_offset)
    return NaverServerClock(reads), reads


def opening_deadline(clock):
    return clock._anchor_monotonic + (BASE_EPOCH + 1000 - clock._anchor_server)


def test_startup_uses_one_read_and_preserves_response_end_anchor(measured_clock):
    clock, reads = measured_clock
    reads.add(0.212)

    assert clock.sync()
    assert reads.calls == 1
    assert clock._anchor_monotonic == pytest.approx(0.212)
    assert clock.last_precision == pytest.approx(0.106)
    assert opening_deadline(clock) == pytest.approx(1000.0)


def test_final_slow_samples_cannot_move_opening_796_ms_later(measured_clock):
    clock, reads = measured_clock
    reads.add(0.212)
    assert clock.sync()
    original_deadline = opening_deadline(clock)
    reads.now = 70.0
    # Synthetic delayed server timestamp: this is not a claim about Naver's
    # actual timestamp-generation phase. Old code accepted this +796 ms shift.
    reads.add(0.796, -0.796, count=5)

    assert clock.sync_precise(5)

    evidence = clock.diagnostic_snapshot()
    assert evidence["status"] == "retained"
    assert evidence["reason"] == "lower_quality"
    assert evidence["candidate_uncertainty_ms"] == pytest.approx(398.0)
    assert evidence["candidate_deadline_shift_ms"] == pytest.approx(796.0)
    assert evidence["applied_deadline_shift_ms"] == 0.0
    assert evidence["uncertainty_ms"] < 120.0
    assert opening_deadline(clock) == original_deadline


def test_one_fast_conflicting_reply_does_not_beat_four_agreeing_replies(measured_clock):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    reads.now = 30.0
    reads.add(0.005, -0.800)
    reads.add(0.080, 0.002, count=4)

    assert clock.sync_precise(5)

    evidence = clock.diagnostic_snapshot()
    assert evidence["status"] == "accepted"
    assert evidence["candidate_rtt_ms"] == pytest.approx(80.0)
    assert evidence["raw_spread_ms"] > 800.0
    assert evidence["candidate_uncertainty_ms"] == pytest.approx(40.0)
    assert abs(evidence["applied_deadline_shift_ms"]) < 3.0


def test_real_clock_step_can_be_adopted_with_good_agreeing_samples(measured_clock):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    reads.now = 30.0
    reads.add(0.080, 0.900, count=3)

    assert clock.sync_precise(3)

    evidence = clock.diagnostic_snapshot()
    assert evidence["status"] == "accepted"
    assert evidence["reason"] == "confirmed_shift"
    assert evidence["applied_deadline_shift_ms"] == pytest.approx(-900.0)
    assert opening_deadline(clock) == pytest.approx(999.1)


def test_large_shift_requires_agreement_across_sparse_single_reads(measured_clock):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    initial_deadline = opening_deadline(clock)
    for index in range(3):
        reads.now = 10.0 + index * 10
        reads.add(0.080, -0.500)
        assert clock.sync()
        evidence = clock.diagnostic_snapshot()
        if index < 2:
            assert evidence["status"] == "retained"
            assert opening_deadline(clock) == initial_deadline
        else:
            assert evidence["status"] == "accepted"
            assert opening_deadline(clock) == pytest.approx(1000.5)


def test_expired_shift_samples_cannot_supply_agreement(measured_clock):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    for moment in (10.0, 80.0, 150.0):
        reads.now = moment
        reads.add(0.080, -0.500)
        assert clock.sync()
        assert clock.diagnostic_snapshot()["status"] == "retained"
    assert opening_deadline(clock) == pytest.approx(1000.0)


def test_stale_anchor_can_refresh_to_consistent_slower_server_clock(measured_clock):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    reads.now = clock.MAX_ANCHOR_AGE_SECONDS + 1.0
    reads.add(0.800, -0.700, count=3)

    assert clock.sync_precise(3)

    evidence = clock.diagnostic_snapshot()
    assert evidence["status"] == "accepted"
    assert evidence["reason"] == "confirmed_shift"
    assert evidence["candidate_uncertainty_ms"] == pytest.approx(400.0)
    assert opening_deadline(clock) == pytest.approx(1000.7)


def test_stale_anchor_does_not_adopt_one_unconfirmed_slow_sample(measured_clock):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    reads.now = clock.MAX_ANCHOR_AGE_SECONDS + 1.0
    reads.add(0.800)

    assert clock.sync()
    assert clock.diagnostic_snapshot()["reason"] == "stale_unconfirmed"
    assert clock._anchor_monotonic == pytest.approx(0.100)


@pytest.mark.parametrize("missing", [True, False])
def test_failed_resync_keeps_existing_clock_and_says_so(measured_clock, missing):
    clock, reads = measured_clock
    messages = []
    clock.log = lambda message, level="info": messages.append(message)
    reads.add(0.100)
    assert clock.sync()
    original_deadline = opening_deadline(clock)
    reads.add(0.100, missing=missing, error=not missing, count=3)

    assert clock.sync_precise(3, announce=True) is False

    assert clock.diagnostic_snapshot()["status"] == "retained"
    assert opening_deadline(clock) == original_deadline
    assert any("기존 서버 시각을 유지" in message for message in messages)
    assert not any("로컬 시계로 진행" in message for message in messages)


def test_history_is_bounded_and_diagnostic_does_not_expose_mutable_state(measured_clock):
    clock, reads = measured_clock
    for _ in range(clock.MAX_HISTORY_SAMPLES + 10):
        reads.add(0.100)
        assert clock.sync()
    snapshot = clock.diagnostic_snapshot()
    assert snapshot["history_sample_count"] == clock.MAX_HISTORY_SAMPLES
    snapshot["samples"][0]["server_epoch_ms"] = 0.0
    assert clock.diagnostic_snapshot()["samples"][0]["server_epoch_ms"] > 0.0
    assert len(snapshot["samples"]) == 1


def test_wall_clock_change_does_not_change_deadline_or_agreement(measured_clock):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    initial_deadline = opening_deadline(clock)
    reads.now = 30.0
    reads.wall_offset = 300.0
    reads.add(0.080, count=3)

    assert clock.sync_precise(3)
    assert opening_deadline(clock) == pytest.approx(initial_deadline)
    assert clock.last_offset == pytest.approx(-300.0)


def test_shared_budget_wait_does_not_contaminate_clock_rtt_or_anchor(measured_clock, monkeypatch):
    _clock, reads = measured_clock
    api = NaverBookingApi("business", "item")

    def acquire(*args, **kwargs):
        reads.now += 0.800

    def post(*args, **kwargs):
        reads.now += 0.100
        stamp = datetime.fromtimestamp(BASE_EPOCH + reads.now, timezone.utc).isoformat()
        return SimpleNamespace(json=lambda: {"data": {"bizItem": {"currentDateTime": stamp}}})

    api.read_coordinator = SimpleNamespace(acquire_read=acquire)
    monkeypatch.setattr(api.session, "post", post)
    clock = NaverServerClock(api)

    assert clock.sync()

    evidence = clock.diagnostic_snapshot()
    assert evidence["rtt_ms"] == pytest.approx(100.0)
    assert evidence["uncertainty_ms"] == pytest.approx(50.0)
    assert evidence["samples"][0]["request_start_monotonic_ms"] == pytest.approx(800.0)
    assert evidence["samples"][0]["response_end_monotonic_ms"] == pytest.approx(900.0)
    assert clock._anchor_monotonic == pytest.approx(0.900)
    assert opening_deadline(clock) == pytest.approx(1000.0)


@pytest.mark.parametrize("shift", [-0.796, 0.796])
def test_final_shift_limit_retains_anchor_despite_good_same_batch_consensus(measured_clock, shift):
    clock, reads = measured_clock
    reads.add(0.212)
    assert clock.sync()
    initial_deadline = opening_deadline(clock)
    reads.now = 30.0
    reads.add(0.080, shift, count=3)

    assert clock.sync_precise(3, max_deadline_shift_ms=100.0)

    evidence = clock.diagnostic_snapshot()
    assert evidence["status"] == "retained"
    assert evidence["reason"] == "final_shift_limit"
    assert evidence["applied_deadline_shift_ms"] == 0.0
    assert opening_deadline(clock) == initial_deadline
    # Outside the final guarded update, a genuine server-clock step can still
    # be adopted from agreeing low-latency measurements.
    reads.add(0.080, shift, count=3)
    assert clock.sync_precise(3)
    assert clock.diagnostic_snapshot()["status"] == "accepted"


def test_final_shift_limit_still_accepts_small_quality_improvement(measured_clock):
    clock, reads = measured_clock
    reads.add(0.212)
    assert clock.sync()
    reads.now = 30.0
    reads.add(0.080, 0.010, count=3)

    assert clock.sync_precise(3, max_deadline_shift_ms=100.0)
    assert clock.diagnostic_snapshot()["status"] == "accepted"
    assert opening_deadline(clock) == pytest.approx(999.99)


def test_shared_read_wait_spends_the_same_timeout_budget_as_http(measured_clock, monkeypatch):
    _clock, reads = measured_clock
    api = NaverBookingApi("business", "item", timeout=1.0)
    captured = {}

    def acquire(*args, **kwargs):
        captured["deadline"] = kwargs["deadline"]
        reads.now += 0.800

    def post(*args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return SimpleNamespace(json=lambda: {"data": {"bizItem": {}}})

    api.read_coordinator = SimpleNamespace(acquire_read=acquire)
    monkeypatch.setattr(api.session, "post", post)

    api.fetch_item_meta()

    assert captured["deadline"] == pytest.approx(1.0)
    assert captured["timeout"] == pytest.approx(0.200)


def test_budget_exhausted_before_http_does_not_send_a_read(measured_clock, monkeypatch):
    _clock, reads = measured_clock
    api = NaverBookingApi("business", "item", timeout=1.0)

    def acquire(*args, **kwargs):
        reads.now += 1.001

    def unexpected_post(*args, **kwargs):
        raise AssertionError("network request sent after its budget expired")

    api.read_coordinator = SimpleNamespace(acquire_read=acquire)
    monkeypatch.setattr(api.session, "post", unexpected_post)

    with pytest.raises(NaverApiError, match="제한 시간이 지났습니다"):
        api.fetch_item_meta()


def test_clock_sampling_uses_one_budget_and_discards_expired_batch(measured_clock):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    original_deadline = opening_deadline(clock)
    original_history = list(clock._samples)
    reads.now = 30.0
    reads.add(0.800, 0.700, count=5)

    assert clock.sync_precise(5, budget_seconds=1.0) is False

    assert reads.calls == 3  # Startup plus two attempts; no third late attempt.
    assert opening_deadline(clock) == original_deadline
    assert clock._samples == original_history
    evidence = clock.diagnostic_snapshot()
    assert evidence["status"] == "retained"
    assert evidence["reason"] == "budget_expired"


def test_late_network_completion_cannot_commit_even_a_precise_clock(measured_clock):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    original_deadline = opening_deadline(clock)

    class LateApi(NaverBookingApi):
        def fetch_item_meta(self, *, deadline=None):
            reads.now += 3.200
            return SimpleNamespace(
                server_time=datetime.fromtimestamp(BASE_EPOCH + reads.now + 0.700, timezone.utc),
                request_started_monotonic=reads.now - 0.010,
                response_end_monotonic=reads.now,
            )

    clock.api = LateApi("business", "item")
    assert clock.sync_precise(3, budget_seconds=3.0) is False

    assert opening_deadline(clock) == original_deadline
    assert clock.diagnostic_snapshot()["samples"][0]["rtt_ms"] == pytest.approx(10.0)
    assert clock.diagnostic_snapshot()["reason"] == "budget_expired"


def test_clock_deadline_is_shared_by_each_read_permit_and_http(measured_clock, monkeypatch):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    original_deadline = opening_deadline(clock)
    reads.now = 10.0
    api = NaverBookingApi("business", "item", timeout=8.0)
    deadlines, timeouts = [], []

    def acquire(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        reads.now += 0.200

    def post(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        reads.now += 0.310
        stamp = datetime.fromtimestamp(BASE_EPOCH + reads.now, timezone.utc).isoformat()
        return SimpleNamespace(json=lambda: {"data": {"bizItem": {"currentDateTime": stamp}}})

    api.read_coordinator = SimpleNamespace(acquire_read=acquire)
    monkeypatch.setattr(api.session, "post", post)
    clock.api = api

    assert clock.sync_precise(5, budget_seconds=1.0, deadline=11.0) is False

    assert deadlines == pytest.approx([11.0, 11.0])
    assert timeouts == pytest.approx([0.800, 0.290])
    assert opening_deadline(clock) == original_deadline


def test_queued_worker_starting_after_absolute_deadline_does_no_reads(measured_clock):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    original_deadline = opening_deadline(clock)
    reads.now = 30.0
    # The caller computed 29.0 before scheduling this delayed worker. A new
    # relative budget must never extend that caller-owned deadline.
    assert clock.sync_precise(5, budget_seconds=3.0, deadline=29.0) is False

    assert reads.calls == 1
    assert opening_deadline(clock) == original_deadline
    assert clock.diagnostic_snapshot()["reason"] == "budget_expired"


def test_precise_clock_can_commit_before_absolute_deadline(measured_clock):
    clock, reads = measured_clock
    reads.add(0.100)
    assert clock.sync()
    reads.now = 30.0
    reads.add(0.080, 0.005, count=3)

    assert clock.sync_precise(3, budget_seconds=3.0, deadline=33.0)
    assert clock.diagnostic_snapshot()["status"] == "accepted"
    assert opening_deadline(clock) == pytest.approx(999.995)
