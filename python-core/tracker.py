import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def init_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)  # 5s busy-wait before raising
    conn.row_factory = sqlite3.Row
    # WAL mode: UI can read while bot is writing — no more cursor spinning.
    # Best-effort: silently skip if another process already holds the DB open.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()
    except Exception:
        pass
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            polymarket_price REAL,
            kalshi_price REAL,
            gap_cents REAL,
            confidence TEXT,
            detected_at TEXT,
            executed INTEGER DEFAULT 0,
            outcome_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gap_id INTEGER REFERENCES gaps(id),
            polymarket_order_id TEXT,
            kalshi_order_id TEXT,
            polymarket_side TEXT,
            kalshi_side TEXT,
            amount_usdc REAL,
            expected_profit REAL,
            actual_profit REAL,
            status TEXT,
            dry_run INTEGER,
            opened_at TEXT,
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS market_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair_type TEXT DEFAULT 'cross_platform',
            token_a TEXT NOT NULL,
            token_b TEXT NOT NULL,
            polymarket_slug TEXT,
            kalshi_ticker TEXT,
            confidence TEXT,
            match_method TEXT,
            gamma_id_a TEXT DEFAULT '',
            gamma_id_b TEXT DEFAULT '',
            outcome_count INTEGER DEFAULT 0,
            times_traded INTEGER DEFAULT 0,
            win_count INTEGER DEFAULT 0,
            created_at TEXT,
            last_seen TEXT,
            UNIQUE(token_a, token_b)
        );

        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        -- Performance indexes — idempotent (must come after CREATE TABLE)
        CREATE INDEX IF NOT EXISTS idx_gaps_detected ON gaps(detected_at);
        CREATE INDEX IF NOT EXISTS idx_gaps_market_detected ON gaps(market_id, detected_at DESC);
        CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);
        CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
        CREATE INDEX IF NOT EXISTS idx_trades_dry_status ON trades(dry_run, status);
    """)
    conn.commit()

    # Migrations — idempotent, safe to run on existing DBs (tables guaranteed to exist now)
    for migration in [
        "ALTER TABLE market_pairs ADD COLUMN outcome_count INTEGER DEFAULT 0",
        "ALTER TABLE gaps ADD COLUMN outcome_count INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass  # Column already exists


_GAP_LOG_COOLDOWN_MINUTES = 5  # log the same market gap at most once every 5 minutes

def log_gap(conn: sqlite3.Connection, gap: dict) -> int:
    """Insert a gap record — skips if same market was already logged in the last 5 min.
    Stores outcome_count directly so the UI query needs no JOIN.
    Returns the existing gap_id on skip, or the new row id on insert."""
    market_id = gap["market_id"]
    # Check for a recent entry for this market
    existing = conn.execute(
        "SELECT id FROM gaps WHERE market_id=? AND detected_at > datetime('now', ?)",
        (market_id, f"-{_GAP_LOG_COOLDOWN_MINUTES} minutes"),
    ).fetchone()
    if existing:
        return existing[0]

    # Look up outcome_count from market_pairs using the token IDs
    token_a = gap.get("polymarket_token", gap.get("token_a", ""))
    token_b = gap.get("kalshi_ticker", gap.get("token_b", ""))
    outcome_count = 0
    if token_a and token_b:
        row = conn.execute(
            "SELECT outcome_count FROM market_pairs WHERE token_a=? AND token_b=?",
            (token_a, token_b),
        ).fetchone()
        if row:
            outcome_count = row[0]

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO gaps
           (market_id, polymarket_price, kalshi_price, gap_cents, confidence, detected_at, outcome_count)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            market_id,
            gap.get("polymarket_price"),
            gap.get("kalshi_price"),
            gap.get("gap_cents"),
            gap.get("confidence", "medium"),
            gap.get("timestamp", now),
            outcome_count,
        ),
    )
    conn.commit()
    return cur.lastrowid


def log_trade(conn: sqlite3.Connection, trade: dict) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO trades
           (gap_id, polymarket_order_id, kalshi_order_id, polymarket_side, kalshi_side,
            amount_usdc, expected_profit, status, dry_run, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trade.get("gap_id"),
            trade.get("polymarket_order_id"),
            trade.get("kalshi_order_id"),
            trade.get("polymarket_side"),
            trade.get("kalshi_side"),
            trade.get("amount_usdc"),
            trade.get("expected_profit"),
            trade.get("status", "open"),
            1 if trade.get("dry_run") else 0,
            trade.get("opened_at", now),
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_trade_result(
    conn: sqlite3.Connection, trade_id: int, actual_profit: float, status: str
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE trades SET actual_profit=?, status=?, resolved_at=? WHERE id=?",
        (actual_profit, status, now, trade_id),
    )
    conn.commit()


def log_market_pair(conn: sqlite3.Connection, pair: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO market_pairs
           (pair_type, token_a, token_b, polymarket_slug, kalshi_ticker,
            confidence, match_method, gamma_id_a, gamma_id_b, outcome_count, created_at, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(token_a, token_b)
           DO UPDATE SET confidence=excluded.confidence, last_seen=excluded.last_seen,
                         pair_type=excluded.pair_type,
                         gamma_id_a=excluded.gamma_id_a,
                         gamma_id_b=excluded.gamma_id_b,
                         outcome_count=excluded.outcome_count""",
        (
            pair.get("pair_type", "cross_platform"),
            pair["token_a"],
            pair["token_b"],
            pair.get("polymarket_slug", ""),
            pair.get("kalshi_ticker", ""),
            pair.get("confidence", "medium"),
            pair.get("match_method", "fuzzy"),
            pair.get("gamma_id_a", ""),
            pair.get("gamma_id_b", ""),
            pair.get("outcome_count", 0),
            now,
            now,
        ),
    )
    conn.commit()


def mark_gap_executed(conn: sqlite3.Connection, gap_id: int) -> None:
    conn.execute("UPDATE gaps SET executed=1 WHERE id=?", (gap_id,))
    conn.commit()


def get_daily_loss(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        """SELECT COALESCE(SUM(actual_profit), 0) FROM trades
           WHERE opened_at > date('now') AND status='resolved' AND actual_profit < 0"""
    ).fetchone()
    return abs(row[0]) if row else 0.0


def get_open_position_count(conn: sqlite3.Connection) -> int:
    # Auto-close dry-run positions older than 1 hour so slots free up
    conn.execute(
        """UPDATE trades SET status='closed', resolved_at=datetime('now'),
           actual_profit=expected_profit
           WHERE status='open' AND dry_run=1
           AND opened_at < datetime('now', '-1 hour')"""
    )
    conn.commit()
    row = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status='open'"
    ).fetchone()
    return row[0] if row else 0


def get_open_live_trades(conn: sqlite3.Connection) -> list[dict]:
    """Return all live (non-dry) open trades with their market_ids."""
    rows = conn.execute(
        """SELECT t.id, g.market_id, t.polymarket_order_id, t.kalshi_order_id,
                  t.polymarket_side, t.kalshi_side,
                  t.amount_usdc, t.expected_profit, t.opened_at
           FROM trades t
           LEFT JOIN gaps g ON g.id = t.gap_id
           WHERE t.status = 'open' AND t.dry_run = 0"""
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_trade(
    conn: sqlite3.Connection,
    trade_id: int,
    actual_profit: float,
    status: str = "resolved",
) -> None:
    """Mark a live trade as resolved with actual profit."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE trades SET actual_profit=?, status=?, resolved_at=? WHERE id=?",
        (actual_profit, status, now, trade_id),
    )
    conn.commit()


def set_bot_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO bot_state(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now),
    )
    conn.commit()


def get_bot_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default
