# PolyyKing — Polymarket / Kalshi Arbitrage Bot

A hybrid Rust + Python prediction market arbitrage bot with a Tauri desktop UI. Rust handles real-time price polling and gap detection. Python handles question matching, multi-layer gap validation, Kelly sizing, live order execution, and trade reconciliation. Tauri provides a live desktop dashboard.

Trades between Polymarket and Kalshi. Finds the same question priced differently on both platforms, bets both sides simultaneously, and locks in risk-adjusted profit when prices converge.

Starts in `DRY_RUN=true` mode — simulates all trades without touching real money until you flip the flag.

---

## How It Works

```
RUST LAYER (fast)
├── Polymarket WS feed              — real-time price feed via Gamma WebSocket
├── Kalshi WS feed                  — real-time order book feed (WebSocket, exponential-backoff reconnect)
├── Price comparator (event-driven) — wakes on price update via watch channel, no polling
└── Bridge                          — stdin/stdout JSON pipe to Python

        ↕ stdin/stdout JSON pipe

PYTHON LAYER (smart)
├── main.py              — entry point, asyncio event loop, per-market cooldown, Prometheus + health servers
├── matcher.py           — links same questions across platforms (fuzzy + exact)
├── detector.py          — 9-layer gap validation (incl. liquidity gate)
├── ev_engine.py         — fee-adjusted net EV calculation
├── kelly_engine.py      — fractional Kelly criterion bet sizing
├── risk_engine.py       — kill switches, exposure limits, drawdown controls
├── bayes_engine.py      — per-market Bayesian posterior updates
├── execution_policy.py  — limit vs market order routing
├── two_leg_executor.py  — concurrent both-leg execution, fill verification, signal capture
├── polymarket_executor.py — Polymarket CLOB API (py_clob_client_v2, L1/L2 auth, get_fill_details)
├── kalshi_executor.py   — Kalshi REST API (HMAC-SHA256 signing, get_fill_details)
├── liquidity.py         — slippage model (power-law market impact)
├── opportunity_engine.py — gap lifecycle state machine (DETECTED→STABLE→EXECUTED/COLLAPSED/EXPIRED)
├── metrics.py           — Prometheus counters/gauges/histograms (gaps, fills, slippage, WS reconnects)
├── health.py            — aiohttp health server on :8080 (ok / degraded)
├── reconciler.py        — async resolution via Gamma API + Kalshi REST
├── calibration.py       — Brier score, EV error, win rate metrics
├── startup_audit.py     — orphan position detection on startup
├── tracker.py           — SQLite trade logging (WAL mode, 8 tables, 11 indexes)
├── notifier.py          — rate-limited loguru terminal + structured JSON log file
└── executor.py          — Rust bridge execution (dry-run + live via Rust subprocess)

TAURI LAYER (UI)
├── src-tauri/src/commands.rs  — 12 Tauri commands, bot lifecycle management
├── src-tauri/src/db.rs        — SQLite read layer, analytics queries
└── src/ (SolidJS)
    ├── App.tsx                — main dashboard (gaps table, trades table, P&L chart)
    ├── components/RiskPanel.tsx        — kill switches, daily loss, open positions
    ├── components/CalibrationPanel.tsx — Brier score, EV error, win rate
    └── components/PortfolioPanel.tsx   — P&L breakdown by category
```

When Rust detects a gap, it writes a JSON event to stdout. Python reads it, validates through 8 layers, sizes the bet using Kelly, executes both legs simultaneously via live APIs (or simulates in dry-run), logs to SQLite, and notifies the terminal. The Tauri UI polls the database every few seconds to display live stats.

---

## What Is Arbitrage

Same question, two platforms, different prices.

Example:
- Polymarket: "Will Fed cut rates in June?" — 71 cents (YES)
- Kalshi: Same question — 58 cents (YES)

You buy YES on Kalshi at 58 cents. You buy NO on Polymarket at 29 cents (1.00 − 0.71). Total spent: 87 cents. When the question resolves, one side pays $1.00. You made 13 cents minus fees. No prediction needed.

The bot does this automatically, many times per day, across all matching markets.

---

## Project Structure

```
PolyyKing/
├── rust-core/
│   ├── Cargo.toml
│   └── src/
│       ├── main.rs               — entry point, spawns tokio tasks
│       ├── types.rs              — Price, Gap, MarketPair, ExecuteCommand, OrderPlaced
│       ├── bridge.rs             — stdin/stdout JSON pipe
│       ├── comparator.rs         — 10ms scan loop, cross-platform + internal gap detection
│       ├── executor.rs           — dry-run + live order placement
│       └── fetcher/
│           ├── polymarket.rs     — Gamma REST price polling
│           └── kalshi.rs         — Kalshi REST price polling (public API)
│
├── python-core/
│   ├── main.py                   — asyncio entry point, per-market cooldown, Prometheus + health servers
│   ├── matcher.py                — fuzzy + exact question matching
│   ├── detector.py               — 9-layer gap validator (incl. liquidity gate)
│   ├── ev_engine.py              — fee-adjusted EV, slippage-aware arb EV
│   ├── kelly_engine.py           — fractional Kelly, arb Kelly (p_exec by confidence tier)
│   ├── risk_engine.py            — RiskEngine: 4 kill switches, exposure cap
│   ├── bayes_engine.py           — BayesEngine: price-delta likelihood ratio updates
│   ├── execution_policy.py       — limit/market order routing by gap size + urgency
│   ├── two_leg_executor.py       — TwoLegExecutor: concurrent both legs, partial fill guard, signal capture
│   ├── polymarket_executor.py    — Polymarket CLOB API (py_clob_client_v2, L1/L2 auth, get_fill_details)
│   ├── kalshi_executor.py        — Kalshi REST API with HMAC-SHA256 signing, get_fill_details
│   ├── liquidity.py              — estimate_slippage_cents() power-law market impact model
│   ├── opportunity_engine.py     — OpportunityEngine: gap lifecycle state machine + DB flush
│   ├── metrics.py                — Prometheus metrics module (counters, gauges, histograms)
│   ├── health.py                 — aiohttp health server on :8080
│   ├── executor.py               — simple Rust bridge executor (used via main.py)
│   ├── reconciler.py             — Reconciler: Gamma + Kalshi resolution polling
│   ├── calibration.py            — Brier score, EV MAE, win rate
│   ├── startup_audit.py          — orphan position cross-check on startup
│   ├── tracker.py                — SQLite init, WAL mode, 8 tables, 11 indexes
│   ├── notifier.py               — rate-limited loguru logging + structured JSONL file
│   └── tests/                    — 18 test files, 154 tests
│       ├── conftest.py
│       ├── test_detector.py      (20 tests)
│       ├── test_tracker.py       (21 tests)
│       ├── test_two_leg_executor.py (19 tests)
│       ├── test_matcher.py       (16 tests)
│       ├── test_reconciler.py    (9 tests)
│       ├── test_kelly_engine.py  (8 tests)
│       ├── test_risk_engine.py   (6 tests)
│       ├── test_ev_engine.py     (6 tests)
│       ├── test_bayes_engine.py  (6 tests)
│       ├── test_polymarket_executor.py (5 tests)
│       ├── test_calibration.py   (4 tests)
│       ├── test_kalshi_executor.py (4 tests)
│       ├── test_startup_audit.py (4 tests)
│       ├── test_metrics.py       (3 tests)
│       ├── test_health.py        (2 tests)
│       ├── test_opportunity_engine.py (7 tests)
│       ├── test_execution_telemetry.py (3 tests)
│       └── test_main_fee_lookup.py (1 test)
│
├── tauri-app/
│   ├── src-tauri/src/
│   │   ├── commands.rs           — 12 Tauri commands
│   │   ├── db.rs                 — SQLite read layer + analytics
│   │   └── lib.rs / main.rs
│   └── src/
│       ├── App.tsx               — main dashboard
│       └── components/
│           ├── RiskPanel.tsx
│           ├── CalibrationPanel.tsx
│           └── PortfolioPanel.tsx
│
├── config/
│   ├── .env.example              — all config variables with defaults
│   └── markets.json              — 126 curated market pairs (16 cross-platform, 110 internal)
│
├── data/
│   └── trades.db                 — SQLite database (auto-created on first run)
│
└── scripts/
    └── backfill_matches.py       — fetches both platforms and populates markets.json
```

---

## Tech Stack

### Rust (fast layer)

```toml
[dependencies]
tokio             = { version = "1.50", features = ["full"] }
serde             = { version = "1.0",  features = ["derive"] }
serde_json        = "1.0"
reqwest           = { version = "0.12", features = ["json", "rustls-tls"] }
tokio-tungstenite = "0.21"    # WebSocket for Kalshi + Polymarket feeds
anyhow            = "1.0"
uuid              = { version = "1",    features = ["v4"] }
chrono            = { version = "0.4",  features = ["serde"] }
env_logger        = "0.11"
dotenv            = "0.15"
log               = "0.4"
crossbeam-channel = "0.5"
```

### Python (smart layer)

```
Python 3.11+
aiohttp            — async HTTP for Kalshi + Gamma API calls, health server
asyncio            — concurrent both-leg execution
sqlite3            — built-in, trade logging
python-dotenv      — API key management
loguru             — clean terminal logging + structured JSONL file output
rapidfuzz          — fuzzy string matching for question matcher
py_clob_client_v2  — Polymarket CLOB API client
prometheus-client  — Prometheus metrics exposition (counters, gauges, histograms)
pytest             — testing
```

### Tauri / SolidJS (UI layer)

```
Tauri 2             — native desktop shell, bot lifecycle management
SolidJS             — reactive UI framework
TanStack Query      — polling with configurable intervals, background sync
@kobalte/core       — accessible dialog + tabs components
@dschz/solid-uplot  — P&L chart
lucide-solid        — icons
TypeScript          — type-safe frontend
```

---

## The Bridge (Rust ↔ Python)

Communication over stdin/stdout JSON, one line per message.

**Rust → Python (gap detected):**
```json
{
  "event": "gap_detected",
  "pair_type": "cross_platform",
  "market_id": "btc-above-95k-june-2026",
  "polymarket_price": 0.29,
  "kalshi_price": 0.58,
  "gap_cents": 13.0,
  "polymarket_token": "0xabc123",
  "kalshi_ticker": "FED-25JUN-T95",
  "timestamp": "2026-05-03T14:22:01Z"
}
```

**Python → Rust (execute order):**
```json
{
  "action": "execute",
  "pair_type": "cross_platform",
  "polymarket_side": "NO",
  "polymarket_amount": 5.00,
  "kalshi_side": "YES",
  "kalshi_amount": 5.00,
  "gap_cents": 13.0,
  "dry_run": true
}
```

**Rust → Python (confirmation):**
```json
{
  "event": "order_placed",
  "polymarket_order_id": "dry_a1b2c3d4",
  "kalshi_order_id": "dry_e5f6g7h8",
  "total_spent": 10.00,
  "expected_profit": 1.09,
  "dry_run": true
}
```

---

## Configuration

Copy `.env.example` to `.env` (or `config/.env`) and fill in your values.

```bash
cp config/.env.example config/.env
```

**`.env.example`:**
```env
# --- MODE ---
DRY_RUN=true                        # true = simulate only, false = real money

# --- POLYMARKET ---
POLYMARKET_PRIVATE_KEY=             # your wallet private key (live mode only)
POLYMARKET_WALLET_ADDRESS=          # your wallet address (live mode only)
POLYMARKET_SIGNATURE_TYPE=0         # 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE
POLYMARKET_GAMMA_URL=https://gamma-api.polymarket.com
POLYMARKET_CLOB_URL=https://clob.polymarket.com

# --- KALSHI ---
KALSHI_API_KEY=                     # from Kalshi dashboard (live mode only)
KALSHI_API_SECRET=                  # from Kalshi dashboard (live mode only)
KALSHI_API_URL=https://api.elections.kalshi.com/trade-api/v2

# --- GAP THRESHOLDS ---
MIN_GAP_CENTS=5                     # ignore gaps smaller than this
MAX_GAP_CENTS=30                    # ignore gaps larger than this (likely data error)
CROSS_PLATFORM_MIN_GAP_CENTS=10     # stricter floor for cross-platform arb
INTERNAL_MIN_GAP_CENTS=8            # floor for internal negRisk arb

# --- EV GATE ---
EV_MIN_CENTS=1.0                    # minimum net EV in cents to execute
EV_TAKER_FEE_RATE=0.02              # 2% taker fee applied to total stake
EV_SLIPPAGE_CENTS=0.5               # expected slippage cost per trade in cents

# --- KELLY SIZING ---
BANKROLL_USDC=500.0                 # total capital across both platforms
MIN_BET_USDC=10                     # minimum bet size per trade
MAX_BET_USDC=100                    # maximum bet size per trade
KELLY_FRACTION=0.25                 # fractional Kelly multiplier (0.25 = quarter-Kelly)

# --- RISK ---
MAX_DAILY_LOSS_USDC=50              # kill switch: stop if daily loss exceeds this
MAX_OPEN_POSITIONS=5                # skip new trades if at this many open positions
MAX_CATEGORY_EXPOSURE_USDC=200      # max total live exposure per market category

# --- RECONCILIATION ---
RECONCILE_INTERVAL_S=300            # how often to poll for resolved trades (seconds)

# --- MISC ---
LOG_LEVEL=INFO
DB_PATH=data/trades.db
MARKETS_JSON=config/markets.json
RUST_BINARY=rust-core/target/release/arb
```

---

## Gap Detection — 9-Layer Validation

`detector.py` validates every gap Rust sends before executing. All layers must pass:

| # | Check | Description |
|---|-------|-------------|
| 0a | Blacklist gate | Skip event IDs in `blacklisted_event_ids` from `markets.json` |
| 0b | Outcome count gate | Internal pairs only: reject if `outcome_count ≠ 2` (multi-outcome negRisk is not safe arb) |
| 1 | Combined price + net EV | `ev_net_cents ≥ EV_MIN_CENTS` — fee + slippage adjusted |
| 1a | Liquidity gate | Reject if `poly_liquidity_usdc` or `kalshi_liquidity_usdc` < `MIN_BET_USDC` (thin market) |
| 2 | Stability | Gap must appear in ≥ 3 consecutive price updates (deque, O(1)) |
| 3 | Stale feed | Skip if feed is marked stale for this market |
| 4 | Resolution proximity | Skip if market closes in < 10 minutes |
| 5 | Daily loss limit | Stop if realized losses exceed `MAX_DAILY_LOSS_USDC` |
| 6 | Open position cap | Skip if at `MAX_OPEN_POSITIONS` (unlimited in dry-run) |
| 7 | Confidence | Never execute `low` confidence pairs |

The liquidity gate rejects gaps where the top-of-book depth on either side is smaller than the minimum bet size. This prevents placing orders that consume >100% of visible liquidity.

---

## EV Engine

`ev_engine.py` computes fee-adjusted net expected value before executing:

```python
# Two-leg arbitrage EV
gap_cents     = (1 - combined) * 100        # gross opportunity in cents
fee_cents     = taker_fee_rate * combined * 100
ev_net_cents  = gap_cents - fee_cents - slippage_cents

# Optional: scale by Bayesian confidence
if p_model is not None:
    confidence_factor = abs(p_model - 0.5) * 2   # 0 = uncertain, 1 = certain
    ev_net_cents *= (0.5 + 0.5 * confidence_factor)
```

Trade rejected if `ev_net_cents < EV_MIN_CENTS`.

---

## Kelly Sizing

`kelly_engine.py` computes bet size using fractional Kelly for arbitrage positions:

```
# Execution probability by confidence tier
p_exec = high: 0.92  |  medium: 0.85  |  low: 0.75

b      = (1 - combined) / combined    # net odds
f*     = (b × p_exec - (1 - p_exec)) / b   # full Kelly fraction
f      = min(f* × 0.25, 0.05)              # quarter-Kelly, capped at 5% of bankroll
bet    = clamp(bankroll × f, MIN_BET_USDC, MAX_BET_USDC)
```

---

## Bayesian Updating

`bayes_engine.py` tracks a per-market posterior probability using price-delta likelihood ratios:

```python
lr        = 1.0 + delta * sensitivity      # delta = new_price - prev_price
posterior = (lr * prior) / (lr * prior + (1 - prior))
```

The posterior is clamped to `[0.01, 0.99]` and optionally fed into `ev_engine.calculate_arb_ev` to scale `ev_net_cents` by market certainty.

---

## Risk Engine

`risk_engine.py` maintains 4 kill switches persisted to the `bot_state` table:

| Switch | Trigger |
|--------|---------|
| `daily_drawdown` | Daily loss exceeds `MAX_DAILY_LOSS_USDC` |
| `api_health` | Exchange API returning errors |
| `model_drift` | EV prediction accuracy degrades beyond threshold |
| `liquidity` | Order book depth below safe fill threshold |

Any active kill switch blocks all trade execution until manually cleared.

---

## Live Execution

`two_leg_executor.py` fires both legs of an arb trade concurrently:

- **Cross-platform**: Polymarket NO + Kalshi YES via `asyncio.gather`
- **Internal**: Polymarket token_a YES + token_b YES via `asyncio.gather`
- **Fill verification**: polls order status up to 30s after placement
- **Partial fill guard**: if one leg fills and the other fails, immediately emergency-closes the filled leg and logs to `emergency_positions` table
- **Balance guard**: checks Polymarket USDC balance before placing order

**Polymarket** (`polymarket_executor.py`): uses `py_clob_client_v2`, two-phase L1/L2 auth. Runs sync SDK in thread executor to avoid blocking asyncio.

**Kalshi** (`kalshi_executor.py`): direct REST API with HMAC-SHA256 request signing. Fully async via `aiohttp`.

---

## Trade Reconciliation

`reconciler.py` runs as a background asyncio task every 5 minutes:

1. Fetches all open live trades from `trades` table
2. Queries Polymarket Gamma API for resolution outcome
3. Falls back to Kalshi REST if Polymarket not yet resolved
4. Computes `actual_profit` (net of fees, deterministic for arb)
5. Updates trade `status` → `profit` or `loss`, writes `actual_profit`

---

## Startup Orphan Audit

`startup_audit.py` runs once at bot startup:

- Fetches all open orders from Polymarket and Kalshi
- Cross-references against `trades` table
- Any exchange position with no matching DB record → inserted into `emergency_positions` as `WARNING` for manual review

---

## Trade Logging Schema

```sql
CREATE TABLE gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    polymarket_price REAL,
    kalshi_price REAL,
    gap_cents REAL,
    confidence TEXT,
    detected_at TEXT,
    executed INTEGER DEFAULT 0,
    outcome_count INTEGER DEFAULT 0   -- stored at insert time, no JOIN needed
);

CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_id INTEGER REFERENCES gaps(id),
    polymarket_order_id TEXT,
    kalshi_order_id TEXT,
    polymarket_side TEXT,
    kalshi_side TEXT,
    amount_usdc REAL,
    expected_profit REAL,       -- NET of fees
    actual_profit REAL,         -- written by reconciler on resolution
    status TEXT,                -- open, profit, loss, closed
    dry_run INTEGER,
    opened_at TEXT,
    resolved_at TEXT
);

CREATE TABLE market_pairs (
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

CREATE TABLE bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
    -- stores kill switch states: ks_daily_drawdown, ks_api_health, etc.
);

CREATE TABLE emergency_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    platform TEXT,
    order_id TEXT,
    side TEXT,
    amount_usdc REAL,
    opened_at TEXT,
    closed_at TEXT,
    status TEXT        -- open, closed_auto
);

-- Tracks every gap opportunity from first detection through resolution
CREATE TABLE opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opp_key TEXT NOT NULL UNIQUE,   -- "{market_id}:{pair_type}"
    market_id TEXT NOT NULL,
    pair_type TEXT NOT NULL DEFAULT 'cross_platform',
    direction TEXT NOT NULL DEFAULT 'dir1',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    first_gap_cents REAL,
    max_gap_cents REAL,
    min_gap_cents REAL,
    avg_gap_cents REAL,
    gap_volatility REAL DEFAULT 0.0,
    observation_count INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'detected',  -- detected, stable, executed, collapsed, expired
    trade_id INTEGER REFERENCES trades(id),
    collapse_reason TEXT,
    poly_bid_size REAL,
    kalshi_ask_size REAL,
    created_at TEXT DEFAULT (datetime('now')),
    closed_at TEXT
);

-- Captures signal, submitted, and fill prices for slippage calibration
CREATE TABLE execution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER REFERENCES trades(id),
    opp_id TEXT,
    pair_type TEXT NOT NULL,
    market_id TEXT NOT NULL,
    signal_poly_price REAL,         -- price at gap detection
    signal_kalshi_price REAL,
    signal_gap_cents REAL,
    signal_poly_liquidity REAL,
    signal_kalshi_liquidity REAL,
    signal_at TEXT NOT NULL,
    submitted_poly_price REAL,      -- price sent to exchange
    submitted_kalshi_price REAL,
    price_buffer_applied REAL DEFAULT 0.0,
    submitted_at TEXT,
    filled_poly_price REAL,         -- actual fill price (written after confirmation)
    filled_kalshi_price REAL,
    filled_at TEXT,
    fill_latency_ms INTEGER,
    poly_slippage_cents REAL,       -- (filled - signal) * 100
    kalshi_slippage_cents REAL,
    total_slippage_cents REAL,
    poly_fill_status TEXT,
    kalshi_fill_status TEXT,
    poly_fill_fraction REAL DEFAULT 1.0,
    kalshi_fill_fraction REAL DEFAULT 1.0,
    is_partial INTEGER DEFAULT 0,
    urgency TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

**Indexes** (auto-created, idempotent):
- `idx_gaps_detected` on `gaps(detected_at)`
- `idx_gaps_market_detected` on `gaps(market_id, detected_at DESC)`
- `idx_trades_opened` on `trades(opened_at)`
- `idx_trades_status` on `trades(status)`
- `idx_trades_dry_status` on `trades(dry_run, status)`
- `idx_opp_key` on `opportunities(opp_key)`
- `idx_opp_state` on `opportunities(state, last_seen DESC)`
- `idx_opp_market` on `opportunities(market_id, state)`
- `idx_exec_trade` on `execution_events(trade_id)`
- `idx_exec_market` on `execution_events(market_id, created_at DESC)`
- `idx_exec_slippage` on `execution_events(total_slippage_cents)`

**Useful queries:**
```bash
# Net P&L today
sqlite3 data/trades.db "SELECT ROUND(SUM(expected_profit),2) FROM trades WHERE opened_at >= date('now');"

# All trades today
sqlite3 data/trades.db "SELECT * FROM trades WHERE opened_at >= date('now') ORDER BY opened_at DESC;"

# Dry-run summary
sqlite3 data/trades.db "SELECT COUNT(*) as trades, ROUND(SUM(expected_profit),2) as net_pnl FROM trades WHERE dry_run=1;"

# Actual P&L on resolved live trades
sqlite3 data/trades.db "SELECT COUNT(*), ROUND(SUM(actual_profit),2) FROM trades WHERE dry_run=0 AND actual_profit IS NOT NULL;"

# Emergency positions requiring review
sqlite3 data/trades.db "SELECT * FROM emergency_positions WHERE status='open';"

# Average slippage per platform (from execution_events)
sqlite3 data/trades.db "SELECT ROUND(AVG(poly_slippage_cents),2) as poly_slip_cents, ROUND(AVG(kalshi_slippage_cents),2) as kalshi_slip_cents FROM execution_events WHERE total_slippage_cents IS NOT NULL;"

# Opportunity lifecycle summary
sqlite3 data/trades.db "SELECT state, COUNT(*) as count, ROUND(AVG(duration_ms)/1000.0,1) as avg_duration_s FROM opportunities GROUP BY state;"

# Prometheus metrics endpoint (while bot is running)
curl -s http://localhost:9090/metrics | grep arb_

# Health check
curl -s http://localhost:8080/health | python3 -m json.tool
```

---

## Setup

### Prerequisites

```bash
# Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Python 3.11+
brew install python@3.11

# uv (fast Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Bun (for Tauri UI)
curl -fsSL https://bun.sh/install | bash
```

### Install

```bash
git clone https://github.com/yourusername/PolyyKing.git
cd PolyyKing

# Python dependencies
cd python-core && uv sync && cd ..

# Rust bot
cd rust-core && cargo build --release && cd ..

# Tauri UI
cd tauri-app && bun install && cd ..

# Config
cp config/.env.example config/.env
# Edit config/.env and fill in your keys
```

### Populate Market Pairs

```bash
# Fetch active markets from both platforms and build config/markets.json
python scripts/backfill_matches.py
```

This fetches Polymarket and Kalshi market data, runs fuzzy matching, and writes 100+ curated pairs with `outcome_count`, `gamma_id`, and confidence scores.

---

## Running

### Option A — Tauri Desktop App (recommended)

```bash
cd tauri-app
bun run tauri dev
```

Click **Start Bot** in the UI. The dashboard polls live data every 2–12 seconds depending on the data type.

### Option B — Terminal only

```bash
cd PolyyKing
python python-core/main.py
```

Expected output:
```
[14:22:01] INFO  | Bot started. DRY_RUN=true. Bankroll: $500
[14:22:03] INFO  | 126 pairs loaded (16 cross-platform, 110 internal). High: 110, Medium: 16
[14:22:03] INFO  | Rust binary started (pid=12345)
[14:22:15] GAP   | 106067::88860102-41022920 | Gap: 13.1¢
[14:22:15] VALID | EV net: 11.2¢ | Kelly: $10.00 | Executing (DRY RUN)
[14:22:15] TRADE | Poly NO $4.35 | Kalshi YES $5.65 | Net: +$1.09
[14:22:15] LOG   | Trade #47 written to trades.db
```

---

## Running Tests

```bash
cd python-core
uv run pytest tests/ -v
# 18 test files, 154 tests
```

Test coverage:
- `test_detector.py` (20) — 9-layer validation, each rejection path, liquidity gate
- `test_tracker.py` (21) — SQLite write/read, bot_state, emergency_positions
- `test_two_leg_executor.py` (19) — concurrent execution, partial fill guard, dry run, fill metrics
- `test_matcher.py` (16) — fuzzy matching, edge cases, confidence scoring
- `test_reconciler.py` (9) — resolution logic, Gamma + Kalshi API mocks
- `test_kelly_engine.py` (8) — Kelly math, arb sizing, edge cases
- `test_opportunity_engine.py` (7) — DETECTED→STABLE→EXECUTED/COLLAPSED/EXPIRED lifecycle
- `test_risk_engine.py` (6) — kill switch persistence, exposure check
- `test_ev_engine.py` (6) — fee calculation, slippage, Bayesian scaling
- `test_bayes_engine.py` (6) — posterior updates, history, clamping
- `test_polymarket_executor.py` (5) — order placement, balance check
- `test_calibration.py` (4) — Brier score, EV MAE, win rate
- `test_kalshi_executor.py` (4) — signing, order placement, error handling
- `test_startup_audit.py` (4) — orphan detection, DB insertion
- `test_metrics.py` (3) — Prometheus counter increments
- `test_health.py` (2) — health endpoint ok/degraded responses
- `test_execution_telemetry.py` (3) — signal logging, slippage computation, dry-run isolation
- `test_main_fee_lookup.py` (1) — CONFIG fee rate sanity

---

## Tauri Dashboard

The desktop app polls the SQLite database directly via Tauri commands. No separate HTTP server.

| Panel | Polls every | Shows |
|-------|-------------|-------|
| Stats bar | 8s | Pairs monitored, gaps today, trades today, P&L |
| Gaps table | 8s | Top 50 arb opportunities (today), sorted by gap size |
| Trades table | 12s | Last 20 trades with row numbers, status, profit |
| P&L chart | 60s | 14-day daily P&L (uPlot) |
| Risk panel | 15s | Kill switches, daily loss, open positions |
| Calibration panel | 60s | Brier score, EV MAE, win rate (resolved live trades) |
| Portfolio panel | 60s | P&L by category (crypto, politics, sports, macro) |

Sync indicator in the top bar only shows "syncing…" if a fetch takes > 800ms (normal polls complete in < 100ms with WAL mode).

Settings dialog lets you configure all poll intervals and set a desktop notification threshold for large gaps.

---

## Going Live

When dry-run results look good (recommend 50+ trades with consistent positive net P&L):

1. Verify Polymarket wallet has USDC funded (Polygon network)
2. Verify Kalshi account has USD funded
3. Set `DRY_RUN=false` in `config/.env`
4. Set `MAX_BET_USDC=10` for the first week
5. Set `MAX_OPEN_POSITIONS=3` to start conservatively
6. Watch terminal output for the first hour; check `emergency_positions` table is empty

**Do not go live until:**
- 50+ dry-run trades show positive net P&L
- You have manually verified at least 5 gap matches by checking both platforms
- High-confidence pairs are driving most of the gap volume

---

## Realistic Numbers

With 126 active pairs, gaps fire 5–30 times per day depending on market conditions. Most are 5–15 cents net of fees. Internal negRisk arbs tend to be more frequent but smaller.

With $500 bankroll, quarter-Kelly sizing at $10 bet:
- Conservative estimate: $5–15 net profit per day in active markets
- The edge shrinks as markets get more efficient — pair quality and execution speed are the moat

---

## Architecture Decisions

**Why stdin/stdout pipe instead of gRPC?**
Zero infrastructure overhead. The gap detection loop is entirely in Rust, Python just needs to receive events and fire orders. Upgrade to gRPC for a VPS deployment with multiple workers.

**Why SQLite instead of Postgres?**
Zero-config, fast enough at this volume with WAL mode. Gap queries run in < 2ms. Swap to Postgres when moving to a server with multiple concurrent writers.

**Why not 100% Rust?**
Gap validation logic (EV gate, Kelly sizing, Bayesian updates, kill switches) changes frequently during tuning. Python lets you iterate without a compile step. Rust handles the latency-critical scan loop.

**Why not 100% Python?**
Running two REST polling loops and a price comparison scan simultaneously — Python's GIL limits concurrency. Rust runs both pollers and the comparator as independent tokio tasks with zero GIL contention.

**Why fractional Kelly?**
Fixed bets ignore edge size. Full Kelly maximises long-run growth but produces large drawdowns (up to 50% bankroll). Quarter-Kelly captures ~80% of the growth rate with much smoother equity curves.

**Why store `outcome_count` in the gaps table?**
The original query joined `gaps` to `market_pairs` on a LIKE match across 80,708 rows — 32 seconds per query. Storing `outcome_count` at insert time reduces the gaps query from 32,000ms to < 2ms with no join needed.

---

## Known Limitations

- Kalshi is US-only (requires US bank/card or ITIN).
- Polymarket requires USDC on Polygon. Bridge fees apply when funding.
- The 2% flat fee is conservative. Real Polymarket fees vary by order type and volume tier. Kalshi uses per-contract cents. Adjust `EV_TAKER_FEE_RATE` after observing your actual fills.
- Price quotes are last-traded prices, not the ask you'll fill at. Slippage reduces profit, especially on Kalshi's thinner book. Adjust `EV_SLIPPAGE_CENTS` based on observed fill quality.
- Question matching is never perfect. 110 internal pairs come from the same negRisk event structure (no cross-platform ambiguity). The 16 cross-platform pairs were manually curated from backfill_matches output. Always verify new cross-platform pairs before going live.
- Partial fill recovery emergency-closes the filled leg at market, taking a small loss. This is rare in dry-run but must be monitored closely in live mode.

---

## Changelog

### May 2026 — Execution Research Platform

**Kalshi WebSocket migration** — `rust-core/src/fetcher/kalshi.rs` rewritten from REST polling to a real-time WebSocket order book feed. Applies snapshot + delta messages to maintain an in-memory `KalshiBook` (BTreeMap). Exponential-backoff reconnect (100ms → 60s cap). Binary frame handler added for protocol completeness.

**Event-driven comparator** — Replaced the 10ms timer loop with a `tokio::sync::watch` channel. The comparator wakes only when a new price is received, eliminating busy-polling and reducing CPU usage. Both liquidity fields (`bid_size`, `ask_size`) are now populated from real order-book data and propagated to Python via the gap event.

**Prometheus observability** (`metrics.py`, `main.py`) — New module exposes all key runtime metrics at `:9090/metrics`:
- `arb_gaps_detected_total` — by pair_type + confidence
- `arb_gaps_rejected_total` — by reason_category + pair_type
- `arb_executions_total` — by pair_type, dry_run, outcome
- `arb_fill_latency_seconds` — histogram per platform
- `arb_gap_to_execution_latency_seconds` — end-to-end latency histogram
- `arb_ws_reconnects_total` — per platform
- `arb_open_positions_count` — live gauge
- `arb_daily_pnl_usdc` — live gauge

**Health server** (`health.py`) — `aiohttp` server on `:8080`. `GET /health` returns `{"status":"ok"|"degraded","last_gap_age_s":N,"open_positions":N}`. Status is `degraded` only after the first gap is seen and no gap has arrived in >120 seconds (startup is always `ok`).

**Structured JSON logs** — `notifier.py` adds a parallel log handler writing serialized JSONL to `logs/arb_structured.jsonl` (100 MB rotation, 7-day retention). Terminal output unchanged.

**Liquidity gate + slippage model** — `rust-core/src/comparator.rs` suppresses gap emission when `ask_size × price < min_bet_usdc` on either side (thin-market guard). `liquidity.py` provides `estimate_slippage_cents(bet_usdc, top_of_book_usdc)` using a power-law market-impact formula. `detector.py` enforces the liquidity gate as layer 1a of the 9-layer validator.

**Opportunity Lifecycle Engine** (`opportunity_engine.py`) — In-memory state machine tracking every gap from first detection through resolution. States: `DETECTED → STABLE (≥3 observations) → EXECUTED / COLLAPSED / EXPIRED`. Per-opportunity stats: avg/max/min gap, volatility, duration. Persisted to `opportunities` table via `tracker.upsert_opportunity()`. Stale eviction background task runs every 60s.

**Execution telemetry** (`execution_events` table) — Every live trade captures signal prices (at gap detection), submitted prices (at order placement), and fill prices (after confirmation). `tracker.log_execution_event()` writes immediately on signal. `tracker.update_execution_fill()` writes fill price and computes per-leg slippage cents. `get_fill_details()` added to both `PolymarketExecutor` and `KalshiExecutor`. Dry-run mode writes no rows.

---

**Complete Python execution stack** — `two_leg_executor.py` replaces the Rust-based executor for live trading. Both legs fire concurrently via `asyncio.gather`. Fill verification polls order status up to 30s. Partial fills trigger immediate emergency close + `emergency_positions` log.

**Live Polymarket integration** — `polymarket_executor.py` implements full L1/L2 auth via `py_clob_client_v2`. Sync SDK runs in thread executor to avoid blocking the event loop. Includes balance guard before order placement.

**Live Kalshi integration** — `kalshi_executor.py` implements HMAC-SHA256 signed REST API. Fully async via `aiohttp`. Handles buy/sell/cancel/status/open-orders.

**Risk engine** — `risk_engine.py` with 4 kill switches (`daily_drawdown`, `api_health`, `model_drift`, `liquidity`) persisted to `bot_state` table. Category exposure limit.

**Bayesian updating** — `bayes_engine.py` tracks per-market posteriors using price-delta likelihood ratios, clamped to `[0.01, 0.99]`. Feeds into EV scaling.

**EV engine** — `ev_engine.py` with fee-adjusted arb EV. Rejects trades where net EV after fee + slippage < `EV_MIN_CENTS`. Optional Bayesian confidence scaling.

**Kelly sizing** — `kelly_engine.py` with fractional Kelly for binary contracts and arb positions. `p_exec` tier by confidence (high: 0.92, medium: 0.85).

**Trade reconciler** — `reconciler.py` polls Polymarket Gamma + Kalshi REST every 5 minutes to resolve open trades and write `actual_profit`.

**Startup orphan audit** — `startup_audit.py` cross-references exchange positions against DB on startup. Orphans logged to `emergency_positions`.

**Calibration metrics** — `calibration.py` computes Brier score, EV prediction MAE, and win rate over resolved live trades. Exposed in Tauri dashboard via `CalibrationPanel`.

**Tauri analytics panels** — `RiskPanel`, `CalibrationPanel`, `PortfolioPanel` added alongside main dashboard. Portfolio panel shows P&L by inferred market category.

**Performance fixes**:
- Gap query: 32,000ms → < 2ms (stored `outcome_count`, `GROUP BY market_id`, removed 80K-row JOIN)
- WAL mode + 3s busy_timeout on all SQLite connections
- 5 covering indexes on trades/gaps tables
- Per-market 60s trade cooldown (monotonic clock, set before `await`)
- asyncio `create_task` for gap handlers (fixes deadlock where `_read_stdout` blocked on its own queue)
- 800ms debounce on "syncing…" indicator (TanStack Query `isFetching` fires on every poll)

**Row numbers** — both gaps table and trades table show `#` as first column.

---

## License

MIT
