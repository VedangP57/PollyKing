import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


async def audit_orphan_positions(poly_executor, kalshi_executor, db_conn) -> None:
    """On startup, cross-reference exchange open positions against trades.db.

    Any position held on an exchange that has no matching open trade in the DB
    is an orphan (likely from a crashed partial fill). These are inserted into
    emergency_positions and logged as WARNING for manual review.
    """
    rows = db_conn.execute(
        "SELECT polymarket_order_id, kalshi_order_id FROM trades WHERE status='open' AND dry_run=0"
    ).fetchall()
    known_poly_ids = {r["polymarket_order_id"] for r in rows if r["polymarket_order_id"]}
    known_kalshi_ids = {r["kalshi_order_id"] for r in rows if r["kalshi_order_id"]}

    now = datetime.now(timezone.utc).isoformat()

    try:
        kalshi_orders = await kalshi_executor.get_open_orders()
        for order in kalshi_orders:
            order_id = order.get("order_id", "")
            if not order_id or order_id in known_kalshi_ids:
                continue
            ticker = order.get("ticker", "unknown")
            count = order.get("count", 0)
            log.warning(
                "ORPHAN KALSHI POSITION: order_id=%s ticker=%s count=%s — not in trades.db",
                order_id, ticker, count,
            )
            db_conn.execute(
                """INSERT INTO emergency_positions
                   (market_id, platform, order_id, side, amount_usdc, opened_at, status)
                   VALUES (?, 'kalshi', ?, ?, ?, ?, 'open')""",
                (ticker, order_id, order.get("side", "unknown"), float(count), now),
            )
        db_conn.commit()
    except Exception as e:
        log.warning("Kalshi orphan audit failed (non-fatal): %s", e)

    try:
        poly_positions = await poly_executor.get_open_positions()
        for pos in poly_positions:
            asset_id = pos.get("asset_id", pos.get("token_id", ""))
            if not asset_id or asset_id in known_poly_ids:
                continue
            size = float(pos.get("size", pos.get("amount", 0)))
            outcome = pos.get("outcome", pos.get("side", "unknown"))
            log.warning(
                "ORPHAN POLYMARKET POSITION: asset_id=%s size=%.4f outcome=%s — not in trades.db",
                asset_id, size, outcome,
            )
            db_conn.execute(
                """INSERT INTO emergency_positions
                   (market_id, platform, order_id, side, amount_usdc, opened_at, status)
                   VALUES (?, 'polymarket', ?, ?, ?, ?, 'open')""",
                (asset_id, asset_id, outcome, size, now),
            )
        db_conn.commit()
    except Exception as e:
        log.warning("Polymarket orphan audit failed (non-fatal): %s", e)
