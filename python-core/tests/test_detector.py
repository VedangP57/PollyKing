import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import tracker
from detector import GapDetector, _is_stable


BASE_GAP = {
    "event": "gap_detected",
    "market_id": "test-market",
    "polymarket_price": 0.71,
    "kalshi_price": 0.58,
    "gap_cents": 13.0,
    "polymarket_token": "token-abc",
    "kalshi_ticker": "TEST-MARKET",
    "timestamp": "2026-05-03T14:22:01Z",
    "confidence": "high",
}


def make_detector(config_overrides=None):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = tracker.init_db(db_path)
    config = {
        "max_daily_loss_usdc": 50.0,
        "max_open_positions": 5,
        **(config_overrides or {}),
    }
    return GapDetector(config, conn), conn


def feed_gap(detector, gap, times=3):
    result = (False, "")
    for _ in range(times):
        result = detector.validate(gap)
    return result


class TestIsStable:
    def test_stable_values(self):
        assert _is_stable([13.0, 13.1, 12.9]) is True

    def test_unstable_values(self):
        assert _is_stable([5.0, 15.0, 13.0]) is False

    def test_single_value(self):
        assert _is_stable([13.0]) is False

    def test_empty(self):
        assert _is_stable([]) is False


class TestValidGap:
    def test_valid_gap_passes_all_checks(self):
        detector, _ = make_detector()
        is_valid, reason = feed_gap(detector, BASE_GAP)
        assert is_valid, f"Expected valid, got: {reason}"

    def test_valid_reason_is_valid(self):
        detector, _ = make_detector()
        _, reason = feed_gap(detector, BASE_GAP)
        assert reason == "valid"


class TestCombinedPriceCheck:
    def test_combined_too_high_rejected(self):
        gap = {**BASE_GAP, "polymarket_price": 0.50, "kalshi_price": 0.50}
        detector, _ = make_detector()
        is_valid, reason = feed_gap(detector, gap)
        assert not is_valid
        assert "0.95" in reason or "fee" in reason.lower()

    def test_combined_exactly_at_limit_rejected(self):
        # poly_no = 0.50, kalshi_yes = 0.45 → combined = 0.95
        gap = {**BASE_GAP, "polymarket_price": 0.50, "kalshi_price": 0.45}
        detector, _ = make_detector()
        is_valid, _ = feed_gap(detector, gap)
        assert not is_valid


class TestStabilityCheck:
    def test_first_update_rejected(self):
        detector, _ = make_detector()
        is_valid, reason = detector.validate(BASE_GAP)
        assert not is_valid
        assert "update" in reason.lower()

    def test_second_update_rejected(self):
        detector, _ = make_detector()
        detector.validate(BASE_GAP)
        is_valid, reason = detector.validate(BASE_GAP)
        assert not is_valid

    def test_third_update_passes(self):
        detector, _ = make_detector()
        feed_gap(detector, BASE_GAP, times=3)
        is_valid, _ = detector.validate(BASE_GAP)
        assert is_valid

    def test_volatile_gap_rejected(self):
        detector, _ = make_detector()
        detector.validate({**BASE_GAP, "gap_cents": 5.0})
        detector.validate({**BASE_GAP, "gap_cents": 20.0})
        is_valid, reason = detector.validate({**BASE_GAP, "gap_cents": 8.0})
        assert not is_valid
        assert "unstable" in reason.lower()


class TestConfidenceCheck:
    def test_low_confidence_rejected(self):
        gap = {**BASE_GAP, "confidence": "low"}
        detector, _ = make_detector()
        is_valid, reason = feed_gap(detector, gap)
        assert not is_valid
        assert "low confidence" in reason.lower()

    def test_medium_confidence_accepted(self):
        gap = {**BASE_GAP, "confidence": "medium"}
        detector, _ = make_detector()
        is_valid, _ = feed_gap(detector, gap)
        assert is_valid


class TestDailyLossLimit:
    def test_loss_limit_blocks_execution(self):
        detector, conn = make_detector({"max_daily_loss_usdc": 0.01})
        # Simulate a large loss already recorded
        gap_id = tracker.log_gap(conn, BASE_GAP)
        trade_id = tracker.log_trade(conn, {
            "gap_id": gap_id,
            "amount_usdc": 100.0,
            "expected_profit": -60.0,
            "dry_run": False,
        })
        tracker.update_trade_result(conn, trade_id, actual_profit=-60.0, status="resolved")

        is_valid, reason = feed_gap(detector, BASE_GAP)
        assert not is_valid
        assert "daily loss" in reason.lower()


class TestOpenPositionLimit:
    def test_max_positions_blocks_execution(self):
        detector, conn = make_detector({"max_open_positions": 0})
        is_valid, reason = feed_gap(detector, BASE_GAP)
        assert not is_valid
        assert "position" in reason.lower()
