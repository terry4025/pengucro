import pytest

from engines.naver_timing import (
    DEFAULT_TARGET_BEFORE_OPEN_SECONDS,
    load_timing_profile,
    record_timing_observation,
)
from pengucro.storage import update_json


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

    assert prepaid.profile.target_before_open_seconds == pytest.approx(0.020)
    assert load_timing_profile(
        "1498729", "7094790", "postpaid"
    ).target_before_open_seconds == pytest.approx(
        DEFAULT_TARGET_BEFORE_OPEN_SECONDS
    )


def test_exhausted_refusal_keeps_ambiguous_arrival_target_unchanged():
    update = record_timing_observation(
        "1498729",
        "7094790",
        "npay_prepaid",
        outcome="refused",
        response_code="RT47",
        inventory_remaining=0,
        timing={"estimated_arrival_offset_ms": -60, "attempts": 1},
    )

    assert update.previous_target_seconds == pytest.approx(0.020)
    assert update.adjustment_seconds == pytest.approx(0.0)
    assert update.profile.target_before_open_seconds == pytest.approx(0.020)


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

    assert first.profile.target_before_open_seconds == pytest.approx(0.020)
    assert success.adjustment_seconds == pytest.approx(0.0)
    assert success.profile.target_before_open_seconds == pytest.approx(0.020)
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
    assert update.profile.target_before_open_seconds == pytest.approx(0.010)


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
    assert update.profile.target_before_open_seconds == pytest.approx(0.020)


def test_v1_ambiguous_early_learning_is_reset_to_safe_prepaid_default():
    key = "1498729|7094790|npay_prepaid"
    update_json(
        "naver_timing_history.json",
        lambda _current: {
            "version": 1,
            "entries": {
                key: {
                    "target_before_open_seconds": 0.070,
                    "observations": [{"outcome": "refused"}],
                }
            },
        },
        {},
    )

    profile = load_timing_profile("1498729", "7094790", "npay_prepaid")

    assert profile.target_before_open_seconds == pytest.approx(0.020)
    assert profile.observation_count == 1


def test_v1_postpaid_explicit_gate_learning_is_preserved():
    key = "1498729|7094790|postpaid"
    update_json(
        "naver_timing_history.json",
        lambda _current: {
            "version": 1,
            "entries": {
                key: {
                    "target_before_open_seconds": 0.050,
                    "observations": [{"outcome": "notopen"}],
                }
            },
        },
        {},
    )

    profile = load_timing_profile("1498729", "7094790", "postpaid")

    assert profile.target_before_open_seconds == pytest.approx(0.050)


def test_migrating_one_product_does_not_reactivate_other_v1_prepaid_targets():
    entries = {
        f"business|{item}|npay_prepaid": {
            "target_before_open_seconds": 0.070,
            "observations": [{"outcome": "refused"}],
        } for item in ("first", "second")
    }
    update_json("naver_timing_history.json", lambda _: {"version": 1, "entries": entries}, {})
    record_timing_observation("business", "first", "postpaid", outcome="success", booking_confirmed=True)
    for item in ("first", "second"):
        assert load_timing_profile("business", item, "npay_prepaid").target_before_open_seconds == pytest.approx(0.02)


def test_previously_mislabelled_v2_prepaid_target_is_also_repaired():
    update_json("naver_timing_history.json", lambda _: {
        "version": 2, "entries": {"b|i|npay_prepaid": {
            "target_before_open_seconds": 0.070, "observations": [],
        }},
    }, {})
    assert load_timing_profile("b", "i", "npay_prepaid").target_before_open_seconds == pytest.approx(0.02)
