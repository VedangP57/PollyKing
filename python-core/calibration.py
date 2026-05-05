"""Calibration and attribution metrics for PolyyKing."""
import sqlite3
import logging
from typing import Optional

log = logging.getLogger(__name__)


def compute_brier_score(conn: sqlite3.Connection, days: int = 30) -> Optional[float]:
    """Brier score over resolved live trades in the last `days` days.

    Predicted probability: 0.5 + (expected_profit / amount_usdc / 2)
    Outcome: 1 if actual_profit > 0, else 0
    """
    rows = conn.execute(
        """SELECT expected_profit, actual_profit, amount_usdc
           FROM trades
           WHERE status IN ('profit', 'loss', 'resolved')
             AND dry_run = 0
             AND actual_profit IS NOT NULL
             AND amount_usdc > 0
             AND opened_at > datetime('now', ?)""",
        (f"-{days} days",),
    ).fetchall()

    if not rows:
        return None

    total = 0.0
    for row in rows:
        expected, actual, amount = row[0], row[1], row[2]
        predicted_p = max(0.0, min(1.0, 0.5 + (expected / amount) / 2.0))
        outcome = 1.0 if actual > 0 else 0.0
        total += (outcome - predicted_p) ** 2

    return round(total / len(rows), 6)


def compute_ev_error(conn: sqlite3.Connection, days: int = 30) -> Optional[float]:
    """Mean absolute error between expected_profit and actual_profit."""
    rows = conn.execute(
        """SELECT expected_profit, actual_profit
           FROM trades
           WHERE status IN ('profit', 'loss', 'resolved')
             AND dry_run = 0
             AND actual_profit IS NOT NULL
             AND opened_at > datetime('now', ?)""",
        (f"-{days} days",),
    ).fetchall()

    if not rows:
        return None

    mae = sum(abs(r[0] - r[1]) for r in rows) / len(rows)
    return round(mae, 6)


def compute_win_rate(conn: sqlite3.Connection, days: int = 30) -> Optional[float]:
    """Fraction of resolved live trades with actual_profit > 0."""
    rows = conn.execute(
        """SELECT actual_profit
           FROM trades
           WHERE status IN ('profit', 'loss', 'resolved')
             AND dry_run = 0
             AND actual_profit IS NOT NULL
             AND opened_at > datetime('now', ?)""",
        (f"-{days} days",),
    ).fetchall()

    if not rows:
        return None

    wins = sum(1 for r in rows if r[0] > 0)
    return round(wins / len(rows), 6)


def get_summary(conn: sqlite3.Connection, days: int = 30) -> dict:
    return {
        "brier_score": compute_brier_score(conn, days),
        "ev_error": compute_ev_error(conn, days),
        "win_rate": compute_win_rate(conn, days),
        "days": days,
    }
