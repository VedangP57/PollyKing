#!/usr/bin/env python3
"""
Integration test: runs the full pipeline with mock data (no real API calls or Rust binary).
Verifies: gap detection → validation → dry-run execution → DB logging.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "python-core"))

import tracker
from detector import GapDetector
from matcher import Matcher, MarketPair

MOCK_POLYMARKET = [
    {"slug": "fed-rate-cut-june-2026", "question": "Will the Fed cut rates at the June 2026 meeting?"},
    {"slug": "btc-above-100k-q3-2026", "question": "Will Bitcoin be above $100k in Q3 2026?"},
    {"slug": "no-match-market", "question": "Some obscure niche question with no match"},
]

MOCK_KALSHI = [
    {"ticker": "FED-25JUN", "title": "Federal Reserve rate cut June 2026 meeting"},
    {"ticker": "BTC-100K-Q3", "title": "Bitcoin above 100k third quarter 2026"},
    {"ticker": "UNRELATED-THING", "title": "Something completely different"},
]

MOCK_GAP = {
    "event": "gap_detected",
    "market_id": "fed-rate-cut-june-2026",
    "polymarket_price": 0.71,
    "kalshi_price": 0.58,
    "gap_cents": 13.0,
    "polymarket_token": "fed-rate-cut-june-2026",
    "kalshi_ticker": "FED-25JUN",
    "timestamp": "2026-05-03T14:22:01Z",
    "confidence": "high",
}


def test_matcher():
    print("[TEST] Matcher...")
    matcher = Matcher.__new__(Matcher)
    matcher.manual_pairs = []
    pairs = matcher.match(MOCK_POLYMARKET, MOCK_KALSHI)

    assert len(pairs) >= 2, f"Expected at least 2 pairs, got {len(pairs)}"

    slugs = [p.polymarket_slug for p in pairs]
    assert "fed-rate-cut-june-2026" in slugs, "Expected fed-rate-cut matched"
    assert "btc-above-100k-q3-2026" in slugs, "Expected btc-above-100k matched"
    assert "no-match-market" not in slugs, "no-match-market should not match"

    print(f"  ✓ Matched {len(pairs)} pairs correctly")


def test_detector():
    print("[TEST] GapDetector...")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db_conn = tracker.init_db(db_path)
    config = {
        "max_daily_loss_usdc": 50.0,
        "max_open_positions": 5,
    }
    detector = GapDetector(config, db_conn)

    # Feed gap 3 times to satisfy stability check
    for _ in range(3):
        is_valid, reason = detector.validate(MOCK_GAP)

    assert is_valid, f"Gap should be valid, got: {reason}"
    print(f"  ✓ Gap validated after 3 updates")

    # Test rejection: combined price too high
    bad_gap = {**MOCK_GAP, "polymarket_price": 0.99, "kalshi_price": 0.99, "gap_cents": -98.0}
    detector2 = GapDetector(config, db_conn)
    for _ in range(3):
        is_valid2, reason2 = detector2.validate(bad_gap)
    assert not is_valid2, "High combined price should be rejected"
    print(f"  ✓ Bad gap rejected: {reason2}")

    db_conn.close()


def test_tracker():
    print("[TEST] Tracker...")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = tracker.init_db(db_path)

    gap_id = tracker.log_gap(conn, MOCK_GAP)
    assert gap_id > 0, "gap_id should be positive"

    trade_id = tracker.log_trade(conn, {
        "gap_id": gap_id,
        "polymarket_order_id": "dry_abc",
        "kalshi_order_id": "dry_xyz",
        "polymarket_side": "NO",
        "kalshi_side": "YES",
        "amount_usdc": 43.50,
        "expected_profit": 6.50,
        "dry_run": True,
    })
    assert trade_id > 0, "trade_id should be positive"

    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    assert row["expected_profit"] == 6.50
    assert row["dry_run"] == 1

    count = conn.execute("SELECT COUNT(*) FROM gaps WHERE market_id=?", ("fed-rate-cut-june-2026",)).fetchone()[0]
    assert count == 1

    conn.close()
    print(f"  ✓ gap_id={gap_id}, trade_id={trade_id}, DB read/write OK")


async def test_full_pipeline():
    print("[TEST] Full pipeline (mock)...")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db_conn = tracker.init_db(db_path)
    config = {
        "dry_run": True,
        "max_daily_loss_usdc": 50.0,
        "max_open_positions": 5,
        "min_bet_usdc": 10.0,
        "max_bet_usdc": 100.0,
    }
    detector = GapDetector(config, db_conn)

    gaps_to_fire = [MOCK_GAP] * 5
    executed = 0

    for gap in gaps_to_fire:
        is_valid, reason = detector.validate(gap)
        if is_valid:
            gap_id = tracker.log_gap(db_conn, gap)
            trade_id = tracker.log_trade(db_conn, {
                "gap_id": gap_id,
                "polymarket_order_id": "dry_test",
                "kalshi_order_id": "dry_test",
                "polymarket_side": "NO",
                "kalshi_side": "YES",
                "amount_usdc": 20.0,
                "expected_profit": gap["gap_cents"] / 100 * 20.0,
                "dry_run": True,
            })
            executed += 1

    assert executed >= 3, f"Expected at least 3 executions, got {executed}"

    rows = db_conn.execute("SELECT COUNT(*) FROM trades WHERE dry_run=1").fetchone()[0]
    assert rows == executed

    db_conn.close()
    print(f"  ✓ {executed} trades logged in dry run pipeline")


def run_all():
    print("\n=== Integration Tests ===\n")
    test_matcher()
    test_detector()
    test_tracker()
    asyncio.run(test_full_pipeline())
    print("\n=== All tests passed ===\n")


if __name__ == "__main__":
    run_all()
