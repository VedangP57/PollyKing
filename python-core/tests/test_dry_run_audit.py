import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent.parent))

import dry_run_audit


def _make_db(trades: list[dict]) -> Path:
    """Create a temp trades.db with the given rows and return its path."""
    db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_file.close()
    path = Path(db_file.name)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            expected_profit REAL,
            actual_profit REAL,
            amount_usdc REAL,
            status TEXT,
            dry_run INTEGER,
            opened_at TEXT
        )"""
    )
    for t in trades:
        conn.execute(
            "INSERT INTO trades (expected_profit, actual_profit, amount_usdc, status, dry_run, opened_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (t["expected_profit"], t.get("actual_profit"), t["amount_usdc"], t["status"], t["dry_run"]),
        )
    conn.commit()
    conn.close()
    return path


def test_no_resolved_trades_exits_2(capsys):
    path = _make_db([
        {"expected_profit": 1.0, "amount_usdc": 20.0, "status": "open", "dry_run": 1},
    ])
    code = dry_run_audit.run(path)
    assert code == 2
    out = capsys.readouterr().out
    assert "insufficient data" in out


def test_good_brier_score_exits_0(capsys):
    # Perfect predictions: expected_profit > 0 and actual_profit > 0 for all
    path = _make_db([
        {"expected_profit": 2.0, "actual_profit": 1.8, "amount_usdc": 20.0, "status": "resolved", "dry_run": 1},
        {"expected_profit": 1.5, "actual_profit": 1.2, "amount_usdc": 15.0, "status": "closed", "dry_run": 1},
    ])
    code = dry_run_audit.run(path)
    assert code == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "Win rate" in out


def test_bad_brier_score_exits_1(capsys):
    # All predictions wrong: expected high profit but actual negative
    path = _make_db([
        {"expected_profit": 5.0, "actual_profit": -1.0, "amount_usdc": 20.0, "status": "resolved", "dry_run": 1},
        {"expected_profit": 5.0, "actual_profit": -2.0, "amount_usdc": 20.0, "status": "resolved", "dry_run": 1},
        {"expected_profit": 5.0, "actual_profit": -1.5, "amount_usdc": 20.0, "status": "resolved", "dry_run": 1},
    ])
    code = dry_run_audit.run(path)
    assert code == 1
    out = capsys.readouterr().out
    assert "FAIL" in out


def test_missing_db_exits_2(capsys):
    code = dry_run_audit.run(Path("/nonexistent/path/trades.db"))
    assert code == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_win_rate_reported_correctly(capsys):
    path = _make_db([
        {"expected_profit": 1.0, "actual_profit": 1.0, "amount_usdc": 20.0, "status": "resolved", "dry_run": 1},
        {"expected_profit": 1.0, "actual_profit": -1.0, "amount_usdc": 20.0, "status": "resolved", "dry_run": 1},
    ])
    dry_run_audit.run(path)
    out = capsys.readouterr().out
    assert "50.0%" in out  # 1 win out of 2


def test_live_trades_not_counted(capsys):
    path = _make_db([
        # live trade (dry_run=0) — should be ignored
        {"expected_profit": 5.0, "actual_profit": 5.0, "amount_usdc": 20.0, "status": "resolved", "dry_run": 0},
        # dry-run open — no resolved data
        {"expected_profit": 1.0, "amount_usdc": 20.0, "status": "open", "dry_run": 1},
    ])
    code = dry_run_audit.run(path)
    assert code == 2  # no resolved dry-run trades
