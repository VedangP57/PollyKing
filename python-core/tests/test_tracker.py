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
