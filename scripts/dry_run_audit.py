"""Dry-run audit — reports EV accuracy and unconfirmed trade attempts.

Usage:
    python scripts/dry_run_audit.py [--db path/to/trades.db]

Exit codes:
    0  — Brier score <= 0.30 (acceptable)
    1  — Brier score > 0.30 (worse than random; do not switch to live)
    2  — No resolved dry-run trades yet (insufficient data)
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path


def _resolve_db(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.getenv("DB_PATH")
    if env:
        return Path(env)
    return Path("data/trades.db")


def _count_trades(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Return (total_dry, resolved_dry, open_dry)."""
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status IN ('resolved', 'closed') THEN 1 ELSE 0 END) AS resolved,
            SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_count
        FROM trades
        WHERE dry_run = 1
        """
    ).fetchone()
    if row is None:
        return 0, 0, 0
    return (row[0] or 0), (row[1] or 0), (row[2] or 0)


def _compute_metrics(conn: sqlite3.Connection) -> tuple[float | None, float | None, float | None]:
    """Return (brier_score, ev_error_cents, win_rate) for resolved dry-run trades."""
    rows = conn.execute(
        """
        SELECT expected_profit, actual_profit, amount_usdc
        FROM trades
        WHERE dry_run = 1
          AND status IN ('resolved', 'closed')
          AND actual_profit IS NOT NULL
          AND amount_usdc > 0
        """
    ).fetchall()

    if not rows:
        return None, None, None

    brier_total = 0.0
    ev_errors = []
    wins = 0

    for expected, actual, amount in rows:
        predicted_p = max(0.0, min(1.0, 0.5 + (expected / amount) / 2.0))
        outcome = 1.0 if actual > 0 else 0.0
        brier_total += (outcome - predicted_p) ** 2
        ev_errors.append(expected - actual)  # positive = predicted too high
        if actual > 0:
            wins += 1

    n = len(rows)
    brier = round(brier_total / n, 6)
    ev_error = round(sum(ev_errors) / n * 100, 3)  # convert to cents
    win_rate = round(wins / n, 6)
    return brier, ev_error, win_rate


def _count_unconfirmed_attempts(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM trade_attempts
            WHERE confirmed_at IS NULL
              AND created_at < datetime('now', '-60 minutes')
            """
        ).fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0  # table may not exist in older DBs


def run(db_path: Path) -> int:
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        total, resolved, open_count = _count_trades(conn)
        brier, ev_error, win_rate = _compute_metrics(conn)
        unconfirmed = _count_unconfirmed_attempts(conn)

        print(f"\nDry-run audit — {total} trades ({resolved} resolved, {open_count} open)")

        if resolved == 0:
            print("No resolved trades yet — insufficient data to score EV accuracy.")
            print(f"Unconfirmed trade_attempts (>60 min): {unconfirmed}")
            return 2

        wr_str = f"{win_rate * 100:.1f}%" if win_rate is not None else "n/a"
        ev_str = f"{ev_error:+.2f}c" if ev_error is not None else "n/a"
        bs_str = f"{brier:.3f}" if brier is not None else "n/a"

        print(f"Win rate:      {wr_str}")
        print(f"Mean EV error: {ev_str}  (positive = predicted too high)")
        print(f"Brier score:   {bs_str}  (lower is better; 0.25 = random)")
        print(f"Unconfirmed trade_attempts (>60 min): {unconfirmed}")

        if brier is not None and brier > 0.30:
            print("\nFAIL: Brier score > 0.30 — EV model is worse than random. Do not switch to live.")
            return 1

        if brier is not None:
            print("\nPASS: EV model is within acceptable range.")

        return 0
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run trade audit")
    parser.add_argument("--db", metavar="PATH", help="Path to trades.db")
    args = parser.parse_args()
    sys.exit(run(_resolve_db(args.db)))


if __name__ == "__main__":
    main()
