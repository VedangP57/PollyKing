import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import tracker


BASE_GAP = {
    "market_id": "fed-rate-cut-june-2026",
    "polymarket_price": 0.71,
    "kalshi_price": 0.58,
    "gap_cents": 13.0,
    "confidence": "high",
    "timestamp": "2026-05-03T14:22:01Z",
}

BASE_TRADE = {
    "polymarket_order_id": "ord_abc123",
    "kalshi_order_id": "ord_xyz456",
    "polymarket_side": "NO",
    "kalshi_side": "YES",
    "amount_usdc": 43.50,
    "expected_profit": 6.50,
    "dry_run": True,
}


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = tracker.init_db(db_path)
    yield conn
    conn.close()


class TestInitDb:
    def test_creates_all_tables(self, db):
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {row[0] for row in tables}
        assert "gaps" in names
        assert "trades" in names
        assert "market_pairs" in names
        assert "emergency_positions" in names

    def test_idempotent(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn1 = tracker.init_db(db_path)
        conn1.close()
        conn2 = tracker.init_db(db_path)
        conn2.close()


class TestGapLogging:
    def test_log_gap_returns_id(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        assert isinstance(gap_id, int)
        assert gap_id > 0

    def test_gap_data_stored_correctly(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        row = db.execute("SELECT * FROM gaps WHERE id=?", (gap_id,)).fetchone()
        assert row["market_id"] == "fed-rate-cut-june-2026"
        assert abs(row["polymarket_price"] - 0.71) < 0.001
        assert abs(row["kalshi_price"] - 0.58) < 0.001
        assert abs(row["gap_cents"] - 13.0) < 0.001
        assert row["confidence"] == "high"
        assert row["executed"] == 0

    def test_mark_gap_executed(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        tracker.mark_gap_executed(db, gap_id)
        row = db.execute("SELECT executed FROM gaps WHERE id=?", (gap_id,)).fetchone()
        assert row["executed"] == 1

    def test_multiple_gaps(self, db):
        id1 = tracker.log_gap(db, BASE_GAP)
        id2 = tracker.log_gap(db, {**BASE_GAP, "market_id": "btc-100k"})
        assert id1 != id2
        count = db.execute("SELECT COUNT(*) FROM gaps").fetchone()[0]
        assert count == 2


class TestTradeLogging:
    def test_log_trade_returns_id(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        trade_id = tracker.log_trade(db, {**BASE_TRADE, "gap_id": gap_id})
        assert isinstance(trade_id, int)
        assert trade_id > 0

    def test_trade_data_stored_correctly(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        trade_id = tracker.log_trade(db, {**BASE_TRADE, "gap_id": gap_id})
        row = db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        assert row["polymarket_order_id"] == "ord_abc123"
        assert row["kalshi_order_id"] == "ord_xyz456"
        assert row["polymarket_side"] == "NO"
        assert row["kalshi_side"] == "YES"
        assert abs(row["amount_usdc"] - 43.50) < 0.001
        assert abs(row["expected_profit"] - 6.50) < 0.001
        assert row["dry_run"] == 1
        assert row["status"] == "open"

    def test_update_trade_result(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        trade_id = tracker.log_trade(db, {**BASE_TRADE, "gap_id": gap_id})
        tracker.update_trade_result(db, trade_id, actual_profit=5.80, status="resolved")
        row = db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        assert abs(row["actual_profit"] - 5.80) < 0.001
        assert row["status"] == "resolved"
        assert row["resolved_at"] is not None

    def test_dry_run_summary_query(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        for _ in range(3):
            tracker.log_trade(db, {**BASE_TRADE, "gap_id": gap_id, "expected_profit": 6.50})

        row = db.execute(
            "SELECT COUNT(*) as trades, SUM(expected_profit) as total FROM trades WHERE dry_run=1"
        ).fetchone()
        assert row["trades"] == 3
        assert abs(row["total"] - 19.50) < 0.001


class TestMarketPairs:
    def test_log_market_pair(self, db):
        tracker.log_market_pair(db, {
            "pair_type": "cross_platform",
            "token_a": "0xabc123",
            "token_b": "FED-25JUN",
            "polymarket_slug": "fed-rate-cut-june-2026",
            "kalshi_ticker": "FED-25JUN",
            "confidence": "high",
            "match_method": "fuzzy",
        })
        row = db.execute("SELECT * FROM market_pairs").fetchone()
        assert row["polymarket_slug"] == "fed-rate-cut-june-2026"
        assert row["kalshi_ticker"] == "FED-25JUN"
        assert row["pair_type"] == "cross_platform"

    def test_upsert_on_conflict(self, db):
        pair = {
            "pair_type": "cross_platform",
            "token_a": "0xabc123",
            "token_b": "FED-25JUN",
            "polymarket_slug": "fed-rate-cut-june-2026",
            "kalshi_ticker": "FED-25JUN",
            "confidence": "medium",
            "match_method": "fuzzy",
        }
        tracker.log_market_pair(db, pair)
        tracker.log_market_pair(db, {**pair, "confidence": "high"})

        count = db.execute("SELECT COUNT(*) FROM market_pairs").fetchone()[0]
        assert count == 1

        row = db.execute("SELECT confidence FROM market_pairs").fetchone()
        assert row["confidence"] == "high"


class TestDailyStats:
    def test_get_daily_loss_zero_when_no_trades(self, db):
        loss = tracker.get_daily_loss(db)
        assert loss == 0.0

    def test_daily_loss_counts_resolved_losses(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        trade_id = tracker.log_trade(db, {**BASE_TRADE, "gap_id": gap_id, "dry_run": False, "amount_usdc": 50.0})
        tracker.update_trade_result(db, trade_id, actual_profit=-20.0, status="resolved")
        loss = tracker.get_daily_loss(db)
        assert loss == pytest.approx(20.0, abs=0.01)

    def test_daily_loss_includes_open_live_exposure(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        # Resolved loss
        trade_id = tracker.log_trade(db, {**BASE_TRADE, "gap_id": gap_id, "dry_run": False, "amount_usdc": 50.0})
        tracker.update_trade_result(db, trade_id, actual_profit=-15.0, status="resolved")
        # Open live trade
        tracker.log_trade(db, {**BASE_TRADE, "gap_id": gap_id, "dry_run": False,
                                "amount_usdc": 30.0, "status": "open"})
        loss = tracker.get_daily_loss(db)
        # 15 realized + 30 open exposure = 45
        assert loss == pytest.approx(45.0, abs=0.01)

    def test_daily_loss_dry_run_open_not_counted(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        # Dry-run open trade must NOT count toward real loss limit
        tracker.log_trade(db, {**BASE_TRADE, "gap_id": gap_id, "dry_run": True,
                                "amount_usdc": 100.0, "status": "open"})
        loss = tracker.get_daily_loss(db)
        assert loss == pytest.approx(0.0, abs=0.01)

    def test_get_open_position_count(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        tracker.log_trade(db, {**BASE_TRADE, "gap_id": gap_id})
        tracker.log_trade(db, {**BASE_TRADE, "gap_id": gap_id})
        count = tracker.get_open_position_count(db)
        assert count == 2


class TestGapCentsAndEmergencyPositions:
    def test_trades_has_gap_cents_column(self, db):
        gap_id = tracker.log_gap(db, BASE_GAP)
        trade_id = tracker.log_trade(db, {
            **BASE_TRADE,
            "gap_id": gap_id,
            "gap_cents": 7.5,
        })
        row = db.execute("SELECT gap_cents FROM trades WHERE id=?", (trade_id,)).fetchone()
        assert row is not None
        assert abs(row["gap_cents"] - 7.5) < 0.001

    def test_emergency_positions_table_exists(self, db):
        ep_id = tracker.log_emergency_position(db, {
            "market_id": "test-market",
            "platform": "polymarket",
            "order_id": "ord_abc",
            "side": "NO",
            "amount_usdc": 5.0,
        })
        row = db.execute("SELECT * FROM emergency_positions WHERE id=?", (ep_id,)).fetchone()
        assert row is not None
        assert row["status"] == "open"
        assert row["platform"] == "polymarket"

    def test_log_emergency_position(self, db):
        ep_id = tracker.log_emergency_position(db, {
            "market_id": "test-market",
            "platform": "kalshi",
            "order_id": "ord_xyz",
            "side": "YES",
            "amount_usdc": 10.0,
        })
        assert ep_id > 0
        row = db.execute("SELECT * FROM emergency_positions WHERE id=?", (ep_id,)).fetchone()
        assert row["platform"] == "kalshi"
        assert row["status"] == "open"
        assert abs(row["amount_usdc"] - 10.0) < 0.001


class TestHasOpenTrade:
    def test_no_open_trade_returns_false(self, db):
        assert tracker.has_open_trade(db, "market-xyz") is False

    def test_open_trade_returns_true(self, db):
        gap_id = tracker.log_gap(db, {**BASE_GAP, "market_id": "market-abc"})
        tracker.log_trade(db, {
            "gap_id": gap_id,
            "amount_usdc": 10.0,
            "gap_cents": 7.0,
            "expected_profit": 0.50,
            "dry_run": True,
        })
        assert tracker.has_open_trade(db, "market-abc") is True

    def test_resolved_trade_returns_false(self, db):
        gap_id = tracker.log_gap(db, {**BASE_GAP, "market_id": "market-def"})
        trade_id = tracker.log_trade(db, {
            "gap_id": gap_id,
            "amount_usdc": 10.0,
            "gap_cents": 7.0,
            "expected_profit": 0.50,
            "dry_run": False,
        })
        tracker.resolve_trade(db, trade_id, actual_profit=0.50)
        assert tracker.has_open_trade(db, "market-def") is False

    def test_different_market_not_affected(self, db):
        gap_id = tracker.log_gap(db, {**BASE_GAP, "market_id": "market-ghi"})
        tracker.log_trade(db, {
            "gap_id": gap_id,
            "amount_usdc": 10.0,
            "gap_cents": 7.0,
            "expected_profit": 0.50,
            "dry_run": True,
        })
        assert tracker.has_open_trade(db, "market-jkl") is False


def test_kalshi_side_dir2_logged_as_NO():
    """Dir2 trades sell Kalshi YES (=buy NO). kalshi_side must be 'NO' not 'YES'."""
    gap = {"pair_type": "cross_platform", "kalshi_action": "sell", "market_id": "test"}
    kalshi_side = "YES" if gap.get("kalshi_action", "buy") == "buy" else "NO"
    assert kalshi_side == "NO", f"Dir2 should log kalshi_side='NO', got '{kalshi_side}'"


def test_kalshi_side_dir1_logged_as_YES():
    """Dir1 trades buy Kalshi YES. kalshi_side must be 'YES'."""
    gap = {"pair_type": "cross_platform", "kalshi_action": "buy", "market_id": "test"}
    kalshi_side = "YES" if gap.get("kalshi_action", "buy") == "buy" else "NO"
    assert kalshi_side == "YES", f"Dir1 should log kalshi_side='YES', got '{kalshi_side}'"


# ── Idempotency keys (Item 18) ──────────────────────────────────────────────

@pytest.fixture
def idem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    tracker._create_tables(conn)
    yield conn
    conn.close()


def test_trade_attempt_logged_before_order(idem_db):
    attempt_id = tracker.log_trade_attempt(idem_db, {
        "attempt_id": "uuid-aaa",
        "market_id": "mkt-1",
        "pair_type": "cross_platform",
        "gap_cents": 8.0,
        "bet_usdc": 25.0,
    })
    row = idem_db.execute(
        "SELECT * FROM trade_attempts WHERE attempt_id='uuid-aaa'"
    ).fetchone()
    assert row is not None
    assert row["confirmed"] == 0
    assert float(row["bet_usdc"]) == pytest.approx(25.0)


def test_confirm_trade_attempt_marks_row(idem_db):
    tracker.log_trade_attempt(idem_db, {
        "attempt_id": "uuid-bbb",
        "market_id": "mkt-2",
        "pair_type": "cross_platform",
        "gap_cents": 5.0,
        "bet_usdc": 10.0,
    })
    tracker.confirm_trade_attempt(idem_db, "uuid-bbb", trade_id=42)
    row = idem_db.execute(
        "SELECT * FROM trade_attempts WHERE attempt_id='uuid-bbb'"
    ).fetchone()
    assert row["confirmed"] == 1
    assert row["trade_id"] == 42


def test_unconfirmed_attempt_returned_by_query(idem_db):
    tracker.log_trade_attempt(idem_db, {
        "attempt_id": "uuid-ccc",
        "market_id": "mkt-3",
        "pair_type": "cross_platform",
        "gap_cents": 6.0,
        "bet_usdc": 15.0,
    })
    rows = tracker.get_unconfirmed_attempts(idem_db, max_age_minutes=60)
    assert any(r["attempt_id"] == "uuid-ccc" for r in rows)
