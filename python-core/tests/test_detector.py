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
        assert "EV" in reason or "ev" in reason.lower()

    def test_combined_high_ev_insufficient_rejected(self):
        # combined = (1-0.50) + 0.48 = 0.98 → ev_cents=2¢, fee≈1.96¢, slippage=0.5¢
        # ev_net ≈ -0.46¢ < ev_min_cents default 1.0¢ → rejected
        gap = {**BASE_GAP, "polymarket_price": 0.50, "kalshi_price": 0.48}
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


def test_cross_platform_gap_uses_per_pair_fee_rate():
    detector, db = make_detector({
        "min_gap_cents": 5, "max_gap_cents": 30,
        "ev_taker_fee_rate": 0.02,  # global default (should be overridden by gap["fee_rate"])
        "ev_min_cents": 1.0, "ev_slippage_cents": 0.5,
        "max_daily_loss_usdc": 50.0, "max_open_positions": 999_999,
        "markets_json": "config/markets.json",
    })
    # 6¢ gap, cross_platform (min=5c) — combined = (1-0.71)+0.58 = 0.87 < 0.95
    gap = {
        "market_id": "test-fee-market",
        "polymarket_price": 0.71,
        "kalshi_price": 0.58,
        "gap_cents": 6.0,
        "confidence": "medium",
        "pair_type": "cross_platform",
        "polymarket_token": "tok123",
        "kalshi_ticker": "TKR123",
        "fee_rate": 0.04,
    }
    ok, reason = feed_gap(detector, gap, times=3)
    assert ok, f"Expected valid: {reason}"


def test_ev_gate_uses_fee_and_slippage(tmp_path):
    """Detector must reject gaps that fail the net EV check after fee + slippage."""
    import sqlite3
    from detector import GapDetector

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    from tracker import _create_tables
    _create_tables(db)

    config = {
        "min_gap_cents": 1.0,
        "max_gap_cents": 30.0,
        "max_daily_loss_usdc": 1000.0,
        "max_open_positions": 999,
        "ev_min_cents": 2.0,
        "ev_taker_fee_rate": 0.02,
        "ev_slippage_cents": 0.5,
    }
    detector = GapDetector(config, db)

    # combined = 0.94 → 6¢ gross gap → ev_net = 6 - 1.88 - 0.5 = 3.62¢ → passes 2¢ threshold
    # cross_platform: combined = (1 - poly_price) + kalshi_price
    # (1 - 0.65) + 0.59 = 0.35 + 0.59 = 0.94
    gap_ok = {
        "market_id": "test-market", "pair_type": "cross_platform",
        "polymarket_price": 0.65, "kalshi_price": 0.59,
        "gap_cents": 6.0, "confidence": "high",
    }
    # Feed 3 consecutive updates to pass the stability check
    for _ in range(3):
        ok, reason = detector.validate(gap_ok)
    assert ok, f"6¢ gap should pass 2¢ EV threshold, got: {reason}"

    # Set high ev_min_cents so the same gap is rejected
    config2 = dict(config)
    config2["ev_min_cents"] = 10.0  # 10¢ minimum — gap only produces ~3.62¢ net
    detector2 = GapDetector(config2, db)
    gap_marginal = {
        "market_id": "test-market-2", "pair_type": "cross_platform",
        "polymarket_price": 0.65, "kalshi_price": 0.59,
        "gap_cents": 6.0, "confidence": "high",
    }
    for _ in range(3):
        ok2, reason2 = detector2.validate(gap_marginal)
    assert not ok2, f"Should reject gap below 10¢ EV threshold"
    assert "EV" in reason2 or "ev" in reason2.lower(), f"Expected EV rejection reason, got: {reason2}"


def test_internal_gap_requires_higher_minimum():
    detector, db = make_detector({
        "ev_taker_fee_rate": 0.02,
        "ev_min_cents": 1.0, "ev_slippage_cents": 0.5,
        "max_daily_loss_usdc": 50.0, "max_open_positions": 999_999,
        "markets_json": "config/markets.json",
        "internal_min_gap_cents": 8.0,
    })
    # Seed outcome_count=2 in market_pairs so binary gate passes
    import tracker
    db.execute(
        "INSERT OR IGNORE INTO market_pairs (token_a, token_b, outcome_count) VALUES (?,?,?)",
        ("tokenA", "tokenB", 2)
    )
    db.commit()
    # 6¢ gap on internal pair — should be rejected (internal min is 8¢)
    gap = {
        "market_id": "99::aaa-bbb",
        "polymarket_price": 0.50,
        "kalshi_price": 0.44,
        "gap_cents": 6.0,
        "confidence": "high",
        "pair_type": "internal",
        "polymarket_token": "tokenA",
        "kalshi_ticker": "tokenB",
        "fee_rate": 0.04,
    }
    ok, reason = feed_gap(detector, gap, times=3)
    assert not ok
    assert "8.0" in reason
