import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import tracker
from tracker import _create_tables, log_gap, log_trade
from startup_audit import audit_orphan_positions


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_tables(conn)
    yield conn
    conn.close()


@pytest.mark.asyncio
async def test_no_orphans_when_positions_match_db(db):
    """Exchange positions that match an open DB trade → no emergency positions logged."""
    gap_id = log_gap(db, {
        "market_id": "fed-rate::abc-def",
        "polymarket_price": 0.70,
        "kalshi_price": 0.22,
        "gap_cents": 8.0,
        "confidence": "high",
    })
    log_trade(db, {
        "gap_id": gap_id,
        "polymarket_order_id": "poly-001",
        "kalshi_order_id": "kal-001",
        "amount_usdc": 10.0,
        "status": "open",
        "dry_run": False,
    })

    mock_poly = AsyncMock()
    mock_poly.get_open_positions.return_value = [
        {"asset_id": "poly-001", "size": 10.0}
    ]
    mock_kalshi = AsyncMock()
    mock_kalshi.get_open_orders.return_value = [
        {"order_id": "kal-001", "ticker": "KXTEST", "count": 10}
    ]

    await audit_orphan_positions(mock_poly, mock_kalshi, db)

    row = db.execute("SELECT COUNT(*) FROM emergency_positions").fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_orphan_kalshi_position_added_to_emergency(db):
    """Kalshi position not in DB → logged as emergency."""
    mock_poly = AsyncMock()
    mock_poly.get_open_positions.return_value = []
    mock_kalshi = AsyncMock()
    mock_kalshi.get_open_orders.return_value = [
        {"order_id": "kal-orphan", "ticker": "KXORPHAN", "count": 5, "side": "yes"}
    ]

    await audit_orphan_positions(mock_poly, mock_kalshi, db)

    row = db.execute(
        "SELECT * FROM emergency_positions WHERE order_id='kal-orphan'"
    ).fetchone()
    assert row is not None
    assert row["platform"] == "kalshi"
    assert row["status"] == "open"


@pytest.mark.asyncio
async def test_orphan_poly_position_added_to_emergency(db):
    """Polymarket position not in DB → logged as emergency."""
    mock_poly = AsyncMock()
    mock_poly.get_open_positions.return_value = [
        {"asset_id": "poly-orphan", "size": 7.5, "outcome": "YES"}
    ]
    mock_kalshi = AsyncMock()
    mock_kalshi.get_open_orders.return_value = []

    await audit_orphan_positions(mock_poly, mock_kalshi, db)

    row = db.execute(
        "SELECT * FROM emergency_positions WHERE order_id='poly-orphan'"
    ).fetchone()
    assert row is not None
    assert row["platform"] == "polymarket"


@pytest.mark.asyncio
async def test_api_failure_does_not_crash_startup(db):
    """If exchange API throws, audit logs warning but does not raise."""
    mock_poly = AsyncMock()
    mock_poly.get_open_positions.side_effect = Exception("network error")
    mock_kalshi = AsyncMock()
    mock_kalshi.get_open_orders.side_effect = Exception("network error")

    # Should not raise
    await audit_orphan_positions(mock_poly, mock_kalshi, db)


@pytest.mark.asyncio
async def test_unconfirmed_attempt_flagged_in_audit(db, caplog):
    """Unconfirmed trade attempts from the last hour are logged as WARNING."""
    import logging
    tracker.log_trade_attempt(db, {
        "attempt_id": "uuid-orphan-test",
        "market_id": "mkt-orphan",
        "pair_type": "cross_platform",
        "gap_cents": 9.0,
        "bet_usdc": 30.0,
    })

    mock_poly = AsyncMock()
    mock_poly.get_open_positions.return_value = []
    mock_kalshi = AsyncMock()
    mock_kalshi.get_open_orders.return_value = []

    with caplog.at_level(logging.WARNING):
        await audit_orphan_positions(mock_poly, mock_kalshi, db)

    assert any("uuid-orphan-test" in r.message for r in caplog.records), (
        "WARNING must include attempt_id of unconfirmed attempt"
    )
