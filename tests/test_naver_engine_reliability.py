"""Engine integration at submission boundaries; no browser or network access."""

import asyncio
import json
import time
from dataclasses import replace

import pytest

from engines.naver_api import NaverAccount, NaverBookingApi, SubmitOutcome, SubmitResult
from engines.naver_engine import NaverEngine
from engines.naver_shared import NaverSharedCoordinator
from engines.naver_submit import (
    NaverArmUncertainError, NaverBrowserSubmitter, NaverSubmitPreparation,
    PAYMENT_NPAY_PREPAID,
)
from test_naver_engine import (
    FakeClock, FakeNpayBookingPage, FakePreparationApi, FakeSubmitter,
    PREPARATION_RESERVATION, make_slot,
)


TARGET = {**PREPARATION_RESERVATION, "reservationDate": "2030-09-13", "reservationTime": "12:50"}
SECRET = "test-session-secret-never-log"


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))


def prepared_engine(tmp_path, results, logs=None):
    engine = NaverEngine(lambda message, _level: logs.append(message) if logs is not None else None)
    engine._shared_reads = NaverSharedCoordinator(tmp_path / "shared", read_interval=0)
    engine._api_account = NaverAccount(True, SECRET, False, user_id="same-test-account")
    engine._api_preparation = NaverSubmitPreparation(
        True, payload={
            "businessId": "1498729", "bizItemId": "7094790",
            "slotId": "1331382668", "csrfToken": SECRET,
        }, slot_id="1331382668",
    )
    engine._api_submitter = FakeSubmitter(results)
    engine._api_submit_enabled = True
    return engine


def success():
    return SubmitResult(SubmitOutcome.SUCCESS, booking_id="999888")


def test_two_engines_share_real_submission_guard_and_only_one_posts(tmp_path):
    logs = []
    first = prepared_engine(tmp_path, [success()], logs)
    second = prepared_engine(tmp_path, [success()], logs)

    async def run():
        return await asyncio.gather(
            first._submit_api_first(reservation_data=TARGET),
            second._submit_api_first(reservation_data=TARGET),
        )

    outcomes = asyncio.run(run())
    assert sorted(outcome for outcome, _detail in outcomes) == ["success", "unknown"]
    assert first._api_submitter.calls + second._api_submitter.calls == 1
    state = json.loads(first._shared_reads.submission_path.read_text(encoding="utf-8"))
    assert len(state) == 1 and next(iter(state.values()))["state"] == "confirmed"
    persisted = json.dumps(state)
    for private in (SECRET, "same-test-account", TARGET["phone"], TARGET["name"]):
        assert private not in persisted and private not in "\n".join(logs)


def test_stop_while_acquiring_disk_guard_releases_without_post(tmp_path):
    engine = prepared_engine(tmp_path, [success()])
    acquire = engine._shared_reads.try_acquire_submission

    def stop_during_acquire(**target):
        lease = acquire(**target)
        engine.stop_event.set()
        return lease

    engine._shared_reads.try_acquire_submission = stop_during_acquire
    result = asyncio.run(engine._submit_api_first(reservation_data=TARGET))
    assert result[0] != "success" and engine._api_submitter.calls == 0
    assert json.loads(engine._shared_reads.submission_path.read_text(encoding="utf-8")) == {}


def test_final_clock_receives_deadline_captured_before_worker_starts():
    from engines.naver_api import NaverServerClock
    engine = NaverEngine(lambda *_args: None)
    engine.clock = NaverServerClock(None)
    seen = {}

    def sync(_count, _announce, **kwargs):
        seen.update(kwargs)
        engine.clock.last_precision = .02
        return True

    engine.clock.sync_precise = sync
    before = time.monotonic()
    assert asyncio.run(engine._sync_clock_before_open())
    assert before + 3.0 <= seen["deadline"] <= time.monotonic() + 3.0
    assert seen["budget_seconds"] == 3.0
    assert seen["max_deadline_shift_ms"] == 100.0


def test_periodic_clock_uses_bounded_consensus_batch_before_final_freeze():
    from engines.naver_api import NaverServerClock
    engine = NaverEngine(lambda *_args: None)
    engine.clock = NaverServerClock(None)
    seen = {}

    def sync(count, _announce, **kwargs):
        seen.update(count=count, **kwargs)
        return True

    engine.clock.sync_precise = sync
    asyncio.run(engine._sync_periodic_clock())
    assert seen["count"] == 3 and seen["budget_seconds"] == 3.0
    assert "deadline" in seen and "max_deadline_shift_ms" not in seen


def test_ready_engine_keeps_optional_reads_out_of_final_opening_window(tmp_path):
    from test_naver_engine import CountdownClock, FakeApi
    engine = prepared_engine(tmp_path, [success()])
    engine.clock = CountdownClock(10.0, step=1.0)
    engine.api = FakeApi(make_slot())
    engine._page = object()
    engine._open_at_epoch = 1.0
    engine._open_strike_pending = True
    engine.notify_success = lambda: None
    strikes = []

    async def strike(*args):
        strikes.append(args)
        return "success", "test booking"

    engine._strike_at_open = strike
    asyncio.run(asyncio.wait_for(engine.make_reservation_async_task(TARGET, 0), timeout=1))
    assert len(strikes) == 1 and engine.api.calls == 0


def test_explicit_not_open_releases_guard_before_bounded_retry(tmp_path):
    engine = prepared_engine(tmp_path, [SubmitResult(SubmitOutcome.NOT_OPEN), success()])
    engine.API_NOT_OPEN_RETRY_SECONDS = 0
    original_submit = engine._api_submitter.submit
    leases = []

    async def submit(payload):
        leases.append(engine._submission_lease.token)
        return await original_submit(payload)

    engine._api_submitter.submit = submit

    async def run():
        result = await engine._submit_api_first(reservation_data=TARGET)
        repeated = await engine._submit_api_first(reservation_data=TARGET)
        return result, repeated

    result, repeated = asyncio.run(run())
    assert result[0] == "success" and repeated[0] == "unknown"
    assert engine._api_submitter.calls == 2
    assert len(set(leases)) == 2


@pytest.mark.parametrize("outcome", [SubmitOutcome.REFUSED, SubmitOutcome.UNKNOWN])
def test_ambiguous_submission_blocks_another_engine_even_when_history_is_empty(tmp_path, outcome):
    first = prepared_engine(tmp_path, [SubmitResult(outcome, code="RT47")])
    second = prepared_engine(tmp_path, [success()])

    async def no_match(**_kwargs):
        return None

    first._reconcile_ambiguous_api_submit = no_match

    async def run():
        return (await first._submit_api_first(reservation_data=TARGET),
                await second._submit_api_first(reservation_data=TARGET))

    results = asyncio.run(run())
    assert all(result[0] == "unknown" for result in results)
    assert first._api_submitter.calls == 1 and second._api_submitter.calls == 0
    state = first._shared_reads.submission_state(**first._submission_lease_identity)
    assert state["state"] == "uncertain"


@pytest.mark.parametrize("cancelled", [True, False])
def test_armed_stop_releases_guard_only_after_positive_browser_cancellation(tmp_path, cancelled):
    first = prepared_engine(tmp_path, [])
    second = prepared_engine(tmp_path, [success()])
    first.clock = FakeClock(1.0)
    first._open_at_epoch = 1.0

    class StoppedArm(FakeSubmitter):
        arm_calls = 0
        cancellations = 0
        async def arm_submit_at(self, _payload, _delay):
            self.arm_calls += 1
            first.stop_event.set()
            return "test-arm"
        async def cancel_armed_submit(self, arm_id):
            assert arm_id == "test-arm"
            self.cancellations += 1
            return cancelled

    first._api_submitter = StoppedArm([])

    async def run():
        return (await first._submit_api_armed(reservation_data=TARGET),
                await second._submit_api_first(reservation_data=TARGET))

    stopped, later = asyncio.run(run())
    assert stopped[0] == "stopped"
    assert first._api_submitter.arm_calls == first._api_submitter.cancellations == 1
    assert first._api_submitter.calls == 0
    assert later[0] == ("success" if cancelled else "unknown")
    assert second._api_submitter.calls == int(cancelled)


def test_lost_arm_reply_keeps_guard_and_never_falls_back_to_second_post(tmp_path):
    first = prepared_engine(tmp_path, [])
    second = prepared_engine(tmp_path, [success()])
    first.clock = FakeClock(1.0)
    first._open_at_epoch = 1.0

    class LostArm(FakeSubmitter):
        async def arm_submit_at(self, _payload, _delay):
            raise NaverArmUncertainError("armed but reply lost")

    async def no_match(**_kwargs):
        return None

    first._api_submitter = LostArm([])
    first._reconcile_ambiguous_api_submit = no_match

    async def run():
        return (await first._submit_api_armed(reservation_data=TARGET),
                await second._submit_api_first(reservation_data=TARGET))

    results = asyncio.run(run())
    assert all(result[0] == "unknown" for result in results)
    assert first._api_submitter.calls == second._api_submitter.calls == 0


def test_optional_account_refresh_timeout_preserves_prepared_payload(tmp_path):
    engine = prepared_engine(tmp_path, [])
    engine.api = FakePreparationApi()
    engine._api_business = engine.api.fetch_business()
    engine._api_biz_item = engine.api.fetch_biz_item_raw()
    engine.API_PREFLIGHT_TOTAL_TIMEOUT_SECONDS = 0.02
    prepared = engine._api_preparation
    cancelled = []

    async def stalled_account():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    engine._api_submitter.fetch_account = stalled_account
    started = time.monotonic()
    assert asyncio.run(engine._refresh_api_submit(TARGET)) is False
    assert time.monotonic() - started < 0.5
    assert cancelled == [True]
    assert engine._api_preparation is prepared and prepared.payload["csrfToken"] == SECRET
    assert engine._api_submit_enabled


def test_post_submit_inventory_uses_fresh_reads_and_separate_timeline_anchors(tmp_path):
    logs = []
    engine = prepared_engine(tmp_path, [], logs)

    class ReadApi(NaverBookingApi):
        def __init__(self):
            self.calls = []
        def find_slot(self, date, time_str, *, fresh=False):
            self.calls.append((date, time_str, fresh))
            return make_slot()

    engine.api = ReadApi()
    engine.API_POST_SUBMIT_INVENTORY_OFFSETS = (0,)
    now = time.monotonic()
    engine._submit_started_monotonic = now - 0.3
    engine._submit_response_monotonic = now - 0.05
    engine._armed_open_monotonic = now - 0.1
    samples = asyncio.run(engine._observe_post_submit_inventory(TARGET))
    assert engine.api.calls == [(TARGET["reservationDate"], TARGET["reservationTime"], True)]
    sample = samples[0]
    assert sample["since_dispatch_ms"] > sample["estimated_open_offset_ms"] > sample["since_response_ms"]
    assert "응답 후 관측 시작 기준" in "\n".join(logs)
    assert SECRET not in "\n".join(logs)


def test_resource_timing_reaches_engine_evidence_without_private_browser_fields(tmp_path):
    engine = prepared_engine(tmp_path, [])

    class Page:
        async def evaluate(self, _script, _argument):
            return {
                "status": "complete", "startedAt": 100, "lastStartedAt": 100,
                "headersAt": 180, "responseBodyAt": 190, "completedAt": 190,
                "response": {"status": 200, "body": {"data": {"submitBooking": {"bookingId": "999888"}}}},
                "attemptTimings": [{"dispatchAt": 100, "requestStart": 103, "responseStart": 180,
                                    "responseEnd": 190, "url": SECRET, "cookie": SECRET}],
            }

    engine._api_submitter = NaverBrowserSubmitter(Page())
    asyncio.run(engine._api_submitter.read_armed_submit("test-arm", {}))
    evidence = engine._timing_observation_payload()
    assert evidence["dispatch_to_request_ms"] == 3
    assert evidence["attempt_timings"][0]["requestStart"] == 103
    assert SECRET not in json.dumps(evidence)


def test_npay_booking_click_timeout_after_send_never_clicks_twice(tmp_path):
    engine = prepared_engine(tmp_path, [])
    engine._page = FakeNpayBookingPage()

    class AmbiguousButton:
        clicks = 0
        async def click(self):
            self.clicks += 1
            raise TimeoutError("reply lost after sending")

    button = AmbiguousButton()
    checkout_calls = []

    async def inspect_checkout(**kwargs):
        checkout_calls.append(kwargs)
        return "unknown", "no confirmed booking"

    engine._continue_npay_checkout = inspect_checkout

    async def run():
        assert await engine._claim_submission(TARGET)
        return await engine._submit_npay(button, dev_mode=True)

    result = asyncio.run(run())
    assert result[0] == "unknown" and button.clicks == 1
    assert len(checkout_calls) == 1 and checkout_calls[0]["booking_id"] == ""
    assert engine._shared_reads.submission_state(**engine._submission_lease_identity)["state"] == "uncertain"


def test_partial_npay_url_opens_evidence_without_payment_control_calls(tmp_path):
    engine = prepared_engine(tmp_path, [])
    engine._api_preparation = replace(engine._api_preparation, payment_mode=PAYMENT_NPAY_PREPAID)
    navigations = []

    class Page:
        async def goto(self, url, **_kwargs):
            navigations.append(url)

    async def no_match(**_kwargs):
        return None

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unconfirmed partial URL must never operate payment controls")

    engine._page = Page()
    engine._reconcile_ambiguous_api_submit = no_match
    engine._continue_npay_checkout = forbidden
    engine._select_npay_money = forbidden
    engine._find_npay_pay_button = forbidden
    candidate = "https://order.pay.naver.com/orderSheet/test"
    result = asyncio.run(engine._handle_api_submit_result(
        SubmitResult(SubmitOutcome.REFUSED, code="RT47", url=candidate),
        signature=None, reservation_data=TARGET, dev_mode=True,
    ))
    assert result[0] == "unknown" and navigations == [candidate]
    assert engine._preserve_checkout_page and not engine._npay_booking_id
