import json
import multiprocessing
import os
import threading
import time
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import engines.naver_shared as shared
from engines.naver_shared import (
    CACHE_MAX_BYTES,
    CACHE_MAX_ENTRIES,
    NaverReadCancelled,
    NaverReadDeadline,
    NaverSharedCoordinator,
    NaverSharedStateError,
)


def _shared_lock_clock(monkeypatch):
    now = [100.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(shared, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=sleep,
    ))
    return now


def test_shared_lock_retries_only_transient_acquisition_errors(tmp_path, monkeypatch):
    now = _shared_lock_clock(monkeypatch)
    acquisitions, exits = [], []

    @contextmanager
    def lock(path, timeout_seconds):
        acquisitions.append(timeout_seconds)
        if len(acquisitions) < 3:
            raise PermissionError("concurrent Windows lock-file initialization")
        try:
            yield
        finally:
            exits.append(True)

    monkeypatch.setattr(shared, "_exclusive_json_lock", lock)
    bodies = []
    with shared._shared_json_lock(tmp_path / "state.json"):
        bodies.append(True)

    assert acquisitions == pytest.approx([.1, .09, .08])
    assert now[0] == pytest.approx(100.02)
    assert bodies == exits == [True]


@pytest.mark.parametrize("failure_stage", ["body", "cleanup"])
def test_shared_lock_never_retries_after_body_entry(tmp_path, monkeypatch, failure_stage):
    acquisitions, bodies = [], []

    @contextmanager
    def lock(path, timeout_seconds):
        acquisitions.append(True)
        yield
        if failure_stage == "cleanup":
            raise PermissionError("write may already have completed")

    monkeypatch.setattr(shared, "_exclusive_json_lock", lock)
    with pytest.raises(PermissionError):
        with shared._shared_json_lock(tmp_path / "state.json"):
            bodies.append(True)
            if failure_stage == "body":
                raise PermissionError("write may already have completed")

    assert acquisitions == bodies == [True]


@pytest.mark.parametrize("budget", [.1, .025])
def test_shared_lock_acquisition_retries_share_one_deadline(tmp_path, monkeypatch, budget):
    now = _shared_lock_clock(monkeypatch)
    acquisitions = []

    @contextmanager
    def lock(path, timeout_seconds):
        acquisitions.append(timeout_seconds)
        raise PermissionError("persistent lock-file error")
        yield

    monkeypatch.setattr(shared, "_exclusive_json_lock", lock)
    with pytest.raises(TimeoutError):
        with shared._shared_json_lock(tmp_path / "state.json", deadline=100 + budget):
            pytest.fail("the protected body must not run")

    assert now[0] == pytest.approx(100 + budget)
    assert acquisitions[0] == pytest.approx(budget)
    assert all(later < earlier for earlier, later in zip(acquisitions, acquisitions[1:]))
    assert 2 <= len(acquisitions) <= 11


def test_shared_lock_releases_late_acquisition_without_entering_body(tmp_path, monkeypatch):
    now = _shared_lock_clock(monkeypatch)
    exits = []

    @contextmanager
    def lock(path, timeout_seconds):
        # The existing common lock helper has a minimum wait interval.
        now[0] += .2
        try:
            yield
        finally:
            exits.append(True)

    monkeypatch.setattr(shared, "_exclusive_json_lock", lock)
    with pytest.raises(TimeoutError):
        with shared._shared_json_lock(tmp_path / "state.json"):
            pytest.fail("an expired acquisition must not enter the protected body")

    assert exits == [True]


def _target(**changes):
    target = dict(profile_identity="test-private-profile", account_id="test-private-account",
                  business_id="1", biz_item_id="2", date_str=(date.today() + timedelta(days=7)).isoformat(),
                  time_str="12:50")
    target.update(changes)
    return target


def _read_worker(directory, start, ready, output):
    coordinator = NaverSharedCoordinator(directory, read_interval=.06)
    ready.put(True)
    start.wait(10)
    coordinator.acquire_read("hourlySchedule", deadline=time.monotonic() + 8)
    output.put(time.monotonic())


def _lease_worker(
    directory, start, ready, output, release, submitted=False,
    account_id="test-private-account",
):
    coordinator = NaverSharedCoordinator(directory)
    ready.put(True)
    start.wait(10)
    lease = coordinator.try_acquire_submission(**_target(account_id=account_id))
    if lease is not None and submitted:
        lease.mark_submitted()
    output.put(lease is not None)
    release.wait(10)


def _finish_processes(processes):
    for process in processes:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_public_reads_share_one_budget_across_spawn_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start, ready, output = ctx.Event(), ctx.Queue(), ctx.Queue()
    processes = [ctx.Process(target=_read_worker, args=(str(tmp_path), start, ready, output))
                 for _ in range(4)]
    for process in processes:
        process.start()
    try:
        assert all(ready.get(timeout=20) for _ in processes)
        start.set()
        granted = sorted(output.get(timeout=12) for _ in processes)
        # Allow a small Windows scheduling/flush margin; four independent
        # budgets would grant all four within a few milliseconds.
        assert granted[-1] - granted[0] >= .15
        assert all(later - earlier >= .035 for earlier, later in zip(granted, granted[1:]))
    finally:
        start.set()
        _finish_processes(processes)
    assert all(process.exitcode == 0 for process in processes)


def test_cancellation_and_deadline_bound_the_read_wait(tmp_path):
    coordinator = NaverSharedCoordinator(tmp_path, read_interval=2)
    coordinator.acquire_read("business")
    with pytest.raises(NaverReadDeadline):
        coordinator.acquire_read("hourlySchedule", deadline=time.monotonic() + .05)
    stop, finished = threading.Event(), threading.Event()
    errors = []

    def waiting():
        try:
            coordinator.acquire_read("hourlySchedule", stop_event=stop)
        except NaverReadCancelled:
            errors.append("cancelled")
        finally:
            finished.set()

    worker = threading.Thread(target=waiting)
    worker.start()
    stop.set()
    assert finished.wait(.5)
    worker.join(1)
    assert errors == ["cancelled"]


@pytest.mark.parametrize("operation", ["submitBooking", "account", "cancelBooking", "unknown"])
def test_submission_and_authenticated_reads_cannot_use_public_budget(tmp_path, operation):
    coordinator = NaverSharedCoordinator(tmp_path)
    with pytest.raises(ValueError):
        coordinator.acquire_read(operation)
    assert not coordinator.budget_path.exists()


def test_budget_recovers_monotonic_origin_after_reboot(tmp_path):
    coordinator = NaverSharedCoordinator(tmp_path)
    coordinator.budget_path.write_text(json.dumps({"last": time.monotonic() + 100000,
                                                  "next_read": time.monotonic() + 100001}))
    assert coordinator.acquire_read("Slot", deadline=time.monotonic() + .2) < .2


def test_cache_reuses_only_public_schedule_and_expires(tmp_path):
    writer = NaverSharedCoordinator(tmp_path)
    reader = NaverSharedCoordinator(tmp_path)
    variables = {"scheduleParams": {"businessId": "1", "bizItemId": "2"}}
    data = {"schedule": {"hourly": [{"stock": 1, "bookingCount": 0}]},
            "__rtt_window__": (1.0, 2.0)}
    assert writer.put_public_read("hourlySchedule", variables, data, ttl=.04)
    cached = reader.get_public_read("hourlySchedule", variables)
    assert cached == {"schedule": data["schedule"]}
    cached["schedule"]["hourly"][0]["stock"] = 99
    assert reader.get_public_read("hourlySchedule", variables)["schedule"]["hourly"][0]["stock"] == 1
    time.sleep(.05)
    assert reader.get_public_read("hourlySchedule", variables) is None


@pytest.mark.parametrize("operation", ["bizItem", "business", "Slot", "account", "submitBooking"])
def test_account_and_clock_data_never_enter_cache(tmp_path, operation):
    coordinator = NaverSharedCoordinator(tmp_path)
    assert not coordinator.put_public_read(operation, {}, {"currentDateTime": "clock", "userId": "private"})
    assert coordinator.get_public_read(operation, {}) is None
    assert not coordinator.cache_path.exists()


@pytest.mark.parametrize("key", ["csrfToken", "cookies", "Authorization", "userId", "phone", "email"])
def test_cache_rejects_sensitive_nested_fields(tmp_path, key):
    coordinator = NaverSharedCoordinator(tmp_path)
    assert not coordinator.put_public_read("hourlySchedule", {}, {"schedule": [{key: "private"}]})
    assert not coordinator.cache_path.exists()


def test_cache_has_bounded_entry_count_and_payload_size(tmp_path):
    coordinator = NaverSharedCoordinator(tmp_path)
    for index in range(CACHE_MAX_ENTRIES + 4):
        assert coordinator.put_public_read("hourlySchedule", {"index": index}, {"stock": 1}, ttl=2)
    assert len(json.loads(coordinator.cache_path.read_text(encoding="utf-8"))) <= CACHE_MAX_ENTRIES
    assert not coordinator.put_public_read("hourlySchedule", {}, {"desc": "x" * CACHE_MAX_BYTES})
    assert coordinator.cache_path.stat().st_size < CACHE_MAX_BYTES


def test_same_slot_has_one_owner_across_spawn_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start, ready, output, release = ctx.Event(), ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [ctx.Process(target=_lease_worker,
                             args=(str(tmp_path), start, ready, output, release)) for _ in range(4)]
    for process in processes:
        process.start()
    try:
        assert all(ready.get(timeout=20) for _ in processes)
        start.set()
        assert sum(output.get(timeout=10) for _ in processes) == 1
    finally:
        start.set()
        release.set()
        _finish_processes(processes)
    assert all(process.exitcode == 0 for process in processes)


@pytest.mark.parametrize("submitted", [False, True])
def test_dead_owner_reclaimed_only_before_submission(tmp_path, submitted):
    ctx = multiprocessing.get_context("spawn")
    start, ready, output, release = ctx.Event(), ctx.Queue(), ctx.Queue(), ctx.Event()
    process = ctx.Process(target=_lease_worker,
                          args=(str(tmp_path), start, ready, output, release, submitted))
    process.start()
    try:
        assert ready.get(timeout=20)
        start.set()
        assert output.get(timeout=10)
    finally:
        release.set()
        _finish_processes([process])
    coordinator = NaverSharedCoordinator(tmp_path)
    lease = coordinator.try_acquire_submission(**_target())
    assert (lease is None) is submitted
    assert coordinator.submission_state(**_target())["state"] == ("submitted" if submitted else "prepared")


def test_same_account_across_profiles_is_protected_and_other_slots_are_independent(tmp_path):
    coordinator = NaverSharedCoordinator(tmp_path)
    lease = coordinator.try_acquire_submission(**_target())
    assert lease is not None
    assert coordinator.try_acquire_submission(**_target(profile_identity="another-private-profile")) is None
    assert coordinator.try_acquire_submission(**_target(time_str="12:50:00")) is None
    assert coordinator.try_acquire_submission(**_target(time_str="14:00")) is not None
    # A reused profile can have incomplete account information in another run,
    # so the same target remains guarded across account changes in that profile.
    assert coordinator.try_acquire_submission(**_target(account_id="another-private-account")) is None
    assert coordinator.try_acquire_submission(**_target(
        account_id="another-private-account", profile_identity="independent-private-profile",
    )) is not None
    raw = coordinator.submission_path.read_text(encoding="utf-8")
    assert "private-account" not in raw
    assert "private-profile" not in raw


@pytest.mark.parametrize("first_id,second_id", [
    ("", "known-private-account"), ("known-private-account", ""),
])
def test_missing_and_known_account_share_profile_submission_guard(tmp_path, first_id, second_id):
    coordinator = NaverSharedCoordinator(tmp_path)
    lease = coordinator.try_acquire_submission(**_target(account_id=first_id))
    lease.mark_submitted()
    lease.finish("uncertain")

    assert coordinator.try_acquire_submission(**_target(account_id=second_id)) is None
    assert coordinator.submission_state(**_target(account_id=second_id))["state"] == "uncertain"


def test_profile_only_guard_learns_account_alias_on_conflict(tmp_path):
    coordinator = NaverSharedCoordinator(tmp_path)
    lease = coordinator.try_acquire_submission(**_target(account_id=""))
    lease.mark_submitted()

    assert coordinator.try_acquire_submission(**_target()) is None
    assert coordinator.try_acquire_submission(**_target(profile_identity="second-private-profile")) is None
    # The denied second-profile lookup also binds its profile alias.
    assert coordinator.try_acquire_submission(**_target(
        profile_identity="second-private-profile", account_id="",
    )) is None


def test_missing_and_known_account_aliases_are_atomic_across_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start, ready, output, release = ctx.Event(), ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [
        ctx.Process(target=_lease_worker, args=(
            str(tmp_path), start, ready, output, release, False, account_id,
        ))
        for account_id in ("", "test-private-account")
    ]
    for process in processes:
        process.start()
    try:
        assert all(ready.get(timeout=20) for _ in processes)
        start.set()
        assert sum(output.get(timeout=10) for _ in processes) == 1
    finally:
        start.set()
        release.set()
        _finish_processes(processes)
    assert all(process.exitcode == 0 for process in processes)


def test_verified_non_send_releases_all_identity_aliases(tmp_path):
    coordinator = NaverSharedCoordinator(tmp_path)
    lease = coordinator.try_acquire_submission(**_target())
    lease.mark_submitted()
    assert coordinator.try_acquire_submission(**_target(profile_identity="second-private-profile")) is None

    assert lease.release_after_no_submission()

    assert coordinator.submission_state(**_target(account_id="")) is None
    assert coordinator.submission_state(**_target(profile_identity="second-private-profile")) is None
    assert coordinator.try_acquire_submission(**_target(
        profile_identity="second-private-profile", account_id="",
    )) is not None


def test_dead_unsubmitted_owner_reclaims_aliases_with_new_token(tmp_path, monkeypatch):
    coordinator = NaverSharedCoordinator(tmp_path)
    old = coordinator.try_acquire_submission(**_target(account_id=""))
    monkeypatch.setattr("engines.naver_shared._pid_alive", lambda pid: False)

    current = coordinator.try_acquire_submission(**_target())

    assert current is not None
    current.mark_submitted()
    with pytest.raises(NaverSharedStateError):
        old.mark_submitted()
    assert coordinator.try_acquire_submission(**_target(account_id="")) is None


def test_live_prepared_owner_is_not_reclaimed_due_to_age(tmp_path):
    coordinator = NaverSharedCoordinator(tmp_path)
    assert coordinator.try_acquire_submission(**_target()) is not None
    state = json.loads(coordinator.submission_path.read_text(encoding="utf-8"))
    for row in state.values():
        row["created"] = time.time() - 86400
        assert row["pid"] == os.getpid()
    coordinator.submission_path.write_text(json.dumps(state), encoding="utf-8")
    assert coordinator.try_acquire_submission(**_target()) is None


@pytest.mark.parametrize("outcome", ["submitted", "uncertain", "confirmed"])
def test_sent_guard_does_not_expire_or_release_without_proof(tmp_path, outcome):
    coordinator = NaverSharedCoordinator(tmp_path)
    lease = coordinator.try_acquire_submission(**_target())
    lease.mark_submitted()
    if outcome != "submitted":
        lease.finish(outcome)
    assert not lease.release_unsubmitted()
    assert coordinator.try_acquire_submission(**_target()) is None
    assert coordinator.submission_state(**_target())["state"] == outcome
    with pytest.raises(NaverSharedStateError):
        lease.mark_submitted()


def test_verified_non_send_allows_another_attempt_and_confirmed_does_not(tmp_path):
    coordinator = NaverSharedCoordinator(tmp_path)
    lease = coordinator.try_acquire_submission(**_target())
    lease.mark_submitted()
    assert lease.release_after_no_submission()
    another = coordinator.try_acquire_submission(**_target())
    another.mark_submitted()
    another.finish("confirmed")
    assert not another.release_after_no_submission()
    assert coordinator.try_acquire_submission(**_target()) is None


def test_unsubmitted_release_and_corrupt_guard_fail_closed(tmp_path):
    coordinator = NaverSharedCoordinator(tmp_path)
    lease = coordinator.try_acquire_submission(**_target())
    assert lease.release_unsubmitted()
    assert coordinator.submission_state(**_target()) is None
    coordinator.submission_path.write_text("truncated{", encoding="utf-8")
    with pytest.raises(NaverSharedStateError):
        coordinator.try_acquire_submission(**_target())


def test_submission_guard_does_not_wait_for_public_budget(tmp_path):
    coordinator = NaverSharedCoordinator(tmp_path, read_interval=60)
    coordinator.acquire_read("hourlySchedule")
    before = time.monotonic()
    lease = coordinator.try_acquire_submission(**_target())
    lease.mark_submitted()
    assert time.monotonic() - before < .5
