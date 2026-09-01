import pytest

from engines.naver_timing import (
    DEFAULT_TARGET_BEFORE_OPEN_SECONDS,
    load_timing_profile,
    record_timing_observation,
)


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))


def test_timing_profiles_are_separate_for_prepaid_and_postpaid():
    prepaid = record_timing_observation(
        "1498729",
        "7094790",
        "npay_prepaid",
        outcome="refused",
        response_code="RT47",
        inventory_remaining=0,
    )

    assert prepaid.profile.target_before_open_seconds == pytest.approx(0.070)
    assert load_timing_profile(
        "1498729", "7094790", "postpaid"
    ).target_before_open_seconds == pytest.approx(
        DEFAULT_TARGET_BEFORE_OPEN_SECONDS
    )


def test_exhausted_refusal_moves_next_arrival_target_earlier_by_ten_ms():
    update = record_timing_observation(
        "1498729",
        "7094790",
        "npay_prepaid",
        outcome="refused",
        response_code="RT47",
        inventory_remaining=0,
        timing={"estimated_arrival_offset_ms": -60, "attempts": 1},
    )

    assert update.previous_target_seconds == pytest.approx(0.060)
    assert update.adjustment_seconds == pytest.approx(0.010)
    assert update.profile.target_before_open_seconds == pytest.approx(0.070)


def test_explicit_not_open_moves_next_arrival_target_later_by_ten_ms():
    update = record_timing_observation(
        "1498729",
        "7094790",
        "postpaid",
        outcome="notopen",
        response_code="BizItem is not opened.",
    )

    assert update.adjustment_seconds == pytest.approx(-0.010)
    assert update.profile.target_before_open_seconds == pytest.approx(0.050)


def test_confirmed_booking_keeps_the_successful_target():
    first = record_timing_observation(
        "1498729",
        "7094790",
        "npay_prepaid",
        outcome="refused",
        inventory_remaining=0,
    )
    success = record_timing_observation(
        "1498729",
        "7094790",
        "npay_prepaid",
        outcome="success",
        booking_confirmed=True,
        inventory_remaining=0,
    )

    assert first.profile.target_before_open_seconds == pytest.approx(0.070)
    assert success.adjustment_seconds == pytest.approx(0.0)
    assert success.profile.target_before_open_seconds == pytest.approx(0.070)
    assert success.profile.observation_count == 2


def test_success_after_explicit_not_open_moves_initial_probe_toward_boundary():
    update = record_timing_observation(
        "1498729",
        "7094790",
        "npay_prepaid",
        outcome="success_after_notopen",
        booking_confirmed=True,
        timing={"attempts": 2, "not_open_attempts": 1},
    )

    assert update.adjustment_seconds == pytest.approx(-0.010)
    assert update.profile.target_before_open_seconds == pytest.approx(0.050)


def test_available_inventory_after_rt47_does_not_guess_or_risk_oscillation():
    update = record_timing_observation(
        "1498729",
        "7094790",
        "npay_prepaid",
        outcome="refused",
        response_code="RT47",
        inventory_remaining=1,
    )

    assert update.adjustment_seconds == pytest.approx(0.0)
    assert update.profile.target_before_open_seconds == pytest.approx(0.060)
