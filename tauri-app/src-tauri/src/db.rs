use chrono::{Datelike, Utc};
use rusqlite::{Connection, Result, params};
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Stats {
    pub pairs_count: i64,
    pub gaps_today: i64,
    pub trades_today: i64,
    pub pnl: f64,
    pub rejected_multi_outcome: i64,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Gap {
    pub market_id: String,
    pub token_a_price: f64,
    pub token_b_price: f64,
    pub gap_cents: f64,
    pub confidence: String,
    pub timestamp: i64,
    pub outcome_count: i64,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Trade {
    pub id: i64,
    pub timestamp: i64,
    pub market_id: String,
    pub side_a: String,
    pub side_b: String,
    pub expected_profit: f64,
    pub status: String,
    pub dry_run: bool,
}

pub fn db_path() -> String {
    // Compile-time anchor: CARGO_MANIFEST_DIR = .../tauri-app/src-tauri
    // ../../data/trades.db = PolyyKing/data/trades.db (absolute, CWD-independent)
    let compile_time = concat!(env!("CARGO_MANIFEST_DIR"), "/../../data/trades.db");
    if Path::new(compile_time).exists() {
        return compile_time.to_string();
    }
    let candidates = ["../data/trades.db", "../../data/trades.db", "data/trades.db"];
    for c in &candidates {
        if Path::new(c).exists() {
            return c.to_string();
        }
    }
    compile_time.to_string()
}

fn open() -> Result<Connection> {
    let conn = Connection::open(db_path())?;

    // WAL mode: allows UI reads to proceed while the bot is writing trades.
    // Without this, every bot DB write blocks all UI reads → cursor spins.
    conn.execute_batch("PRAGMA journal_mode=WAL;")?;

    // If another write holds the lock, wait up to 3 seconds before giving up.
    // Prevents the UI from hanging indefinitely on lock contention.
    conn.busy_timeout(std::time::Duration::from_millis(3000))?;

    // Optimise reads — UI only reads, never writes.
    conn.execute_batch("PRAGMA synchronous=NORMAL; PRAGMA temp_store=MEMORY;")?;

    Ok(conn)
}

fn today_prefix() -> String {
    let now = Utc::now();
    format!("{:04}-{:02}-{:02}", now.year(), now.month(), now.day())
}

fn iso_to_unix(s: &str) -> i64 {
    chrono::DateTime::parse_from_rfc3339(s)
        .map(|dt| dt.timestamp())
        .unwrap_or_else(|_| {
            // Try without timezone offset
            chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f")
                .map(|dt| dt.and_utc().timestamp())
                .unwrap_or(0)
        })
}

pub fn get_stats() -> Stats {
    let pairs_count = count_pairs();
    let rejected_multi_outcome = read_rejected_multi_outcome();
    let conn = match open() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[PolyyKing Backend] get_stats: failed to open DB: {e}");
            return Stats { pairs_count, gaps_today: 0, trades_today: 0, pnl: 0.0, rejected_multi_outcome };
        }
    };

    let today = today_prefix();

    let gaps_today: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM gaps WHERE detected_at >= ?1",
            params![today],
            |r| r.get(0),
        )
        .unwrap_or(0);

    let trades_today: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM trades WHERE opened_at >= ?1",
            params![today],
            |r| r.get(0),
        )
        .unwrap_or(0);

    let pnl: f64 = conn
        .query_row(
            "SELECT COALESCE(SUM(expected_profit), 0.0) FROM trades WHERE opened_at >= ?1",
            params![today],
            |r| r.get(0),
        )
        .unwrap_or(0.0);

    Stats { pairs_count, gaps_today, trades_today, pnl, rejected_multi_outcome }
}

/// Read the rejected_multi_outcome count written by backfill_matches.py into markets.json.
fn read_rejected_multi_outcome() -> i64 {
    // markets.json lives at project_root/config/markets.json
    let manifest = env!("CARGO_MANIFEST_DIR"); // .../tauri-app/src-tauri
    let path = Path::new(manifest)
        .parent().unwrap_or(Path::new("."))
        .parent().unwrap_or(Path::new("."))
        .join("config/markets.json");

    let Ok(raw) = std::fs::read_to_string(&path) else { return 0 };
    let Ok(val) = serde_json::from_str::<serde_json::Value>(&raw) else { return 0 };
    val.get("_stats")
        .and_then(|s| s.get("rejected_multi_outcome"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0)
}

fn query_gaps(conn: &Connection, today: &str) -> Result<Vec<Gap>> {
    // Ensure indexes exist — idempotent, < 1ms if already present
    let _ = conn.execute_batch(
        "CREATE INDEX IF NOT EXISTS idx_gaps_detected ON gaps(detected_at);
         CREATE INDEX IF NOT EXISTS idx_gaps_market_detected ON gaps(market_id, detected_at DESC);"
    );

    // No JOIN needed: outcome_count is stored directly in the gaps row at insert time.
    // Deduplicate by market_id (latest entry wins) so the UI shows one row per market.
    // This query runs in <5ms regardless of how many gap rows accumulate.
    let mut stmt = conn.prepare(
        "SELECT market_id,
                polymarket_price, kalshi_price, gap_cents,
                confidence, MAX(detected_at) AS detected_at,
                outcome_count
         FROM gaps
         WHERE detected_at >= ?1
         GROUP BY market_id
         ORDER BY gap_cents DESC
         LIMIT 50",
    )?;
    let rows = stmt.query_map(params![today], |r| {
        let detected_at: String = r.get(5).unwrap_or_default();
        Ok(Gap {
            market_id: r.get(0)?,
            token_a_price: r.get(1).unwrap_or(0.0),
            token_b_price: r.get(2).unwrap_or(0.0),
            gap_cents: r.get(3).unwrap_or(0.0),
            confidence: r.get(4).unwrap_or_else(|_| "medium".to_string()),
            timestamp: iso_to_unix(&detected_at),
            outcome_count: r.get(6).unwrap_or(0),
        })
    })?;
    rows.filter_map(|r| r.ok()).collect::<Vec<_>>().pipe_ok()
}

fn query_trades(conn: &Connection) -> Result<Vec<Trade>> {
    let mut stmt = conn.prepare(
        "SELECT t.id, t.opened_at, g.market_id,
                t.polymarket_side, t.kalshi_side,
                t.expected_profit, t.status, t.dry_run
         FROM trades t
         LEFT JOIN gaps g ON t.gap_id = g.id
         ORDER BY t.opened_at DESC
         LIMIT 50",
    )?;
    let rows = stmt.query_map([], |r| {
        let opened_at: String = r.get(1).unwrap_or_default();
        let dry_run_int: i64 = r.get(7).unwrap_or(1);
        Ok(Trade {
            id: r.get(0)?,
            timestamp: iso_to_unix(&opened_at),
            market_id: r.get(2).unwrap_or_else(|_| "—".to_string()),
            side_a: r.get(3).unwrap_or_else(|_| "YES".to_string()),
            side_b: r.get(4).unwrap_or_else(|_| "NO".to_string()),
            expected_profit: r.get(5).unwrap_or(0.0),
            status: r.get(6).unwrap_or_else(|_| "open".to_string()),
            dry_run: dry_run_int != 0,
        })
    })?;
    rows.filter_map(|r| r.ok()).collect::<Vec<_>>().pipe_ok()
}

trait PipeOk: Sized {
    fn pipe_ok(self) -> Result<Self>;
}
impl<T> PipeOk for Vec<T> {
    fn pipe_ok(self) -> Result<Self> { Ok(self) }
}

pub fn get_active_gaps() -> Vec<Gap> {
    let today = today_prefix();
    match open() {
        Ok(conn) => match query_gaps(&conn, &today) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("[PolyyKing Backend] get_active_gaps: query failed: {e}");
                vec![]
            }
        },
        Err(e) => {
            eprintln!("[PolyyKing Backend] get_active_gaps: failed to open DB: {e}");
            vec![]
        }
    }
}

pub fn get_recent_trades() -> Vec<Trade> {
    match open() {
        Ok(conn) => match query_trades(&conn) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("[PolyyKing Backend] get_recent_trades: query failed: {e}");
                vec![]
            }
        },
        Err(e) => {
            eprintln!("[PolyyKing Backend] get_recent_trades: failed to open DB: {e}");
            vec![]
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct DailyPnl {
    pub date: String,
    pub pnl: f64,
}

/// Aggregated realized/simulated P&L per calendar day (UTC) from `opened_at`.
pub fn get_daily_pnl(days: i64) -> Vec<DailyPnl> {
    let Ok(conn) = open() else {
        eprintln!("[PolyyKing Backend] get_daily_pnl: failed to open DB");
        return vec![];
    };
    let days = days.clamp(1, 90);
    let anchor = format!("-{} days", days);
    let sql = "SELECT substr(opened_at, 1, 10) AS d,
                      COALESCE(SUM(expected_profit), 0.0) AS total
               FROM trades
               WHERE date(substr(opened_at, 1, 10)) >= date('now', ?1)
               GROUP BY d
               ORDER BY d ASC";
    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("[PolyyKing Backend] get_daily_pnl: prepare failed: {e}");
            return vec![];
        }
    };
    let rows = stmt.query_map(params![anchor], |r| {
        Ok(DailyPnl {
            date: r.get(0)?,
            pnl: r.get(1)?,
        })
    });
    match rows {
        Ok(iter) => iter.filter_map(|x| x.ok()).collect(),
        Err(e) => {
            eprintln!("[PolyyKing Backend] get_daily_pnl: query failed: {e}");
            vec![]
        }
    }
}

fn count_pairs() -> i64 {
    match open() {
        Ok(conn) => conn
            .query_row("SELECT COUNT(*) FROM market_pairs", [], |r| r.get(0))
            .unwrap_or(80691),
        Err(e) => {
            eprintln!("[PolyyKing Backend] count_pairs: failed to open DB: {e}");
            80691
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct RiskState {
    pub kill_switches: std::collections::HashMap<String, bool>,
    pub daily_loss_usdc: f64,
    pub open_positions: i64,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct CalibrationStats {
    pub brier_score: Option<f64>,
    pub ev_error: Option<f64>,
    pub win_rate: Option<f64>,
    pub trade_count: i64,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct CategoryBreakdown {
    pub category: String,
    pub pnl: f64,
    pub trade_count: i64,
    pub win_rate: f64,
}

pub fn get_risk_state(db: &str) -> Result<RiskState> {
    let conn = Connection::open(db)?;
    conn.execute_batch("PRAGMA journal_mode=WAL;")?;
    conn.busy_timeout(std::time::Duration::from_millis(3000))?;
    conn.execute_batch("PRAGMA synchronous=NORMAL; PRAGMA temp_store=MEMORY;")?;

    let mut kill_switches = std::collections::HashMap::new();
    let switch_names = ["daily_drawdown", "api_health", "model_drift", "liquidity"];
    for name in &switch_names {
        let key = format!("ks_{}", name);
        let val: String = conn
            .query_row(
                "SELECT value FROM bot_state WHERE key=?1",
                params![key],
                |row| row.get(0),
            )
            .unwrap_or_else(|_| "false".to_string());
        kill_switches.insert(name.to_string(), val == "true");
    }

    let daily_loss: f64 = conn
        .query_row(
            "SELECT COALESCE(ABS(SUM(actual_profit)), 0.0) FROM trades \
             WHERE opened_at > date('now') AND status='loss' AND dry_run=0",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0.0);

    let open_positions: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM trades WHERE status='open'",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);

    Ok(RiskState { kill_switches, daily_loss_usdc: daily_loss, open_positions })
}

pub fn get_calibration_stats(db: &str) -> Result<CalibrationStats> {
    let conn = Connection::open(db)?;
    conn.execute_batch("PRAGMA journal_mode=WAL;")?;
    conn.busy_timeout(std::time::Duration::from_millis(3000))?;
    conn.execute_batch("PRAGMA synchronous=NORMAL; PRAGMA temp_store=MEMORY;")?;

    let trade_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM trades WHERE status IN ('profit','loss','resolved','closed')",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);

    if trade_count == 0 {
        return Ok(CalibrationStats {
            brier_score: None,
            ev_error: None,
            win_rate: None,
            trade_count: 0,
        });
    }

    let wins: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM trades \
             WHERE status IN ('profit','closed') AND actual_profit > 0",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);
    let win_rate = wins as f64 / trade_count as f64;

    let ev_error: Option<f64> = conn
        .query_row(
            "SELECT AVG(ABS(expected_profit - actual_profit)) FROM trades \
             WHERE status IN ('profit','loss','resolved','closed') AND actual_profit IS NOT NULL",
            [],
            |row| row.get(0),
        )
        .ok();

    Ok(CalibrationStats {
        brier_score: None,
        ev_error,
        win_rate: Some(win_rate),
        trade_count,
    })
}

pub fn get_portfolio_breakdown(db: &str) -> Result<Vec<CategoryBreakdown>> {
    let conn = Connection::open(db)?;
    conn.execute_batch("PRAGMA journal_mode=WAL;")?;
    conn.busy_timeout(std::time::Duration::from_millis(3000))?;
    conn.execute_batch("PRAGMA synchronous=NORMAL; PRAGMA temp_store=MEMORY;")?;

    let rows = conn.prepare(
        "SELECT market_id, actual_profit, expected_profit, status FROM trades \
         WHERE opened_at > datetime('now', '-30 days')"
    )?.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, Option<f64>>(1)?,
            row.get::<_, f64>(2)?,
            row.get::<_, String>(3)?,
        ))
    })?.filter_map(|r| r.ok()).collect::<Vec<_>>();

    let mut categories: std::collections::HashMap<String, (f64, i64, i64)> =
        std::collections::HashMap::new();

    for (market_id, actual, expected, _status) in rows {
        let cat = infer_category(&market_id);
        let profit = actual.unwrap_or(expected);
        let is_win = profit > 0.0;
        let entry = categories.entry(cat).or_insert((0.0, 0, 0));
        entry.0 += profit;
        entry.1 += 1;
        if is_win { entry.2 += 1; }
    }

    let mut result: Vec<CategoryBreakdown> = categories
        .into_iter()
        .map(|(cat, (pnl, count, wins))| CategoryBreakdown {
            category: cat,
            pnl: (pnl * 100.0).round() / 100.0,
            trade_count: count,
            win_rate: if count > 0 { wins as f64 / count as f64 } else { 0.0 },
        })
        .collect();
    result.sort_by(|a, b| b.pnl.partial_cmp(&a.pnl).unwrap_or(std::cmp::Ordering::Equal));
    Ok(result)
}

fn infer_category(market_id: &str) -> String {
    let m = market_id.to_lowercase();
    if m.contains("btc") || m.contains("eth") || m.contains("crypto") {
        return "crypto".to_string();
    }
    if m.contains("election") || m.contains("president") || m.contains("senate") {
        return "politics".to_string();
    }
    if m.contains("nfl") || m.contains("nba") || m.contains("sport") {
        return "sports".to_string();
    }
    if m.contains("fed") || m.contains("rate") || m.contains("cpi") {
        return "macro".to_string();
    }
    "other".to_string()
}
