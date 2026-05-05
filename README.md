# PolyyKing — Polymarket / Kalshi Arbitrage Bot

A hybrid Rust + Python prediction market arbitrage bot with a Tauri desktop UI. Rust handles real-time price feeds and order execution. Python handles question matching, gap validation, Kelly sizing, and trade logging. Tauri provides a live dashboard.

Trades between Polymarket and Kalshi. Finds the same question priced differently on both platforms, bets both sides simultaneously, and locks in risk-free profit when prices converge.

Starts in `DRY_RUN=true` mode — simulates all trades without touching real money until you flip the flag.

---

## How It Works

```
RUST LAYER (fast)
├── Polymarket WebSocket listener     — real-time price feed
├── Kalshi WebSocket listener         — real-time price feed
├── Price comparator (hot loop)       — runs thousands of times per second
└── Order executor                    — places both bets simultaneously

        ↕ stdin/stdout JSON pipe (microseconds)

PYTHON LAYER (smart)
├── matcher.py        — links same questions across platforms
├── detector.py       — 7-layer gap validation (EV gate, cooldown, confidence)
├── ev_engine.py      — expected value calculation, fee-adjusted net EV
├── kelly_engine.py   — fractional Kelly criterion bet sizing
├── executor.py       — dry run / live execution logic
├── execution_policy.py — order type and urgency decisions
├── tracker.py        — SQLite trade logging + bot state
├── reconciler.py     — async trade resolution via Polymarket Gamma API
├── bayes_engine.py   — Bayesian posterior updates per market
├── notifier.py       — terminal logging via loguru
└── main.py           — entry point, runs everything

TAURI LAYER (UI)
├── src-tauri/src/commands.rs  — Tauri commands, bot process management
├── src-tauri/src/db.rs        — SQLite queries for dashboard data
└── src/ (SolidJS)
    ├── App.tsx                — main dashboard, TanStack Query polling
    ├── KpiBar.tsx             — live KPI strip (pairs, gaps, trades, P&L)
    ├── GapsTable.tsx          — arbitrage opportunities table
    ├── TradesTable.tsx        — recent trades (up to 500)
    ├── RiskPanel.tsx          — daily loss, drawdown, open positions
    ├── CalibrationPanel.tsx   — win rate, EV accuracy, Brier score
    ├── PortfolioPanel.tsx     — category P&L breakdown
    └── PnlChart.tsx           — daily P&L chart
```

When Rust detects a gap, it writes a JSON event to stdout. Python reads it, validates it through 7 layers, decides whether to execute, then writes back. Rust places both orders simultaneously. Python logs the result.

---

## What Is Arbitrage

Same question, two platforms, different prices.

Example:
- Polymarket: "Will Fed cut rates in June?" — 71 cents (YES)
- Kalshi: Same question — 58 cents (YES)

You buy YES on Kalshi at 58 cents. You buy NO on Polymarket at 29 cents (1.00 - 0.71). Total spent: 87 cents. When the question resolves, one side pays $1.00. You made 13 cents minus fees. Guaranteed. No prediction needed.

The bot does this automatically, many times per day, across all matching markets.

---

## Project Structure

```
PolyyKing/
├── rust-core/                    — Rust workspace
│   ├── Cargo.toml
│   └── src/
│       ├── main.rs               — entry point, spawns tasks
│       ├── fetcher/
│       │   ├── polymarket.rs     — Polymarket WebSocket listener
│       │   └── kalshi.rs         — Kalshi WebSocket listener
│       ├── comparator.rs         — hot loop, gap detection
│       ├── executor.rs           — order placement, fee-net profit calculation
│       ├── bridge.rs             — stdin/stdout JSON pipe to Python
│       └── types.rs              — shared structs (Price, Gap, Order)
│
├── python-core/                  — Python workspace
│   ├── main.py                   — entry point, starts Rust subprocess
│   ├── matcher.py                — question matching across platforms
│   ├── detector.py               — 7-layer gap validation
│   ├── ev_engine.py              — fee-adjusted EV calculation
│   ├── kelly_engine.py           — fractional Kelly criterion sizing
│   ├── executor.py               — dry run / live execution logic
│   ├── execution_policy.py       — order type and urgency
│   ├── tracker.py                — SQLite trade logging + bot state
│   ├── reconciler.py             — trade resolution via Gamma API
│   ├── bayes_engine.py           — Bayesian market posterior
│   ├── notifier.py               — loguru terminal logger
│   └── tests/
│       ├── conftest.py
│       ├── test_matcher.py
│       ├── test_detector.py
│       ├── test_tracker.py
│       ├── test_ev_engine.py
│       └── test_reconciler.py
│
├── tauri-app/                    — Desktop UI (Tauri + SolidJS)
│   ├── src-tauri/
│   │   ├── src/
│   │   │   ├── main.rs
│   │   │   ├── commands.rs       — Tauri commands, bot lifecycle
│   │   │   └── db.rs             — SQLite read layer for dashboard
│   └── src/                      — SolidJS frontend
│
├── config/
│   ├── .env.example              — all config variables with defaults
│   └── markets.json              — 5,644 curated market pair lines
│
├── data/
│   └── trades.db                 — SQLite database (auto-created on first run)
│
└── README.md
```

---

## Tech Stack

### Rust (fast layer)

```toml
[dependencies]
tokio = { version = "1.50", features = ["full"] }
tokio-tungstenite = { version = "0.24", features = ["native-tls"] }
serde = { version = "1.0", features = ["derive"] }
serde_json = "1.0"
reqwest = { version = "0.12", features = ["json", "rustls-tls"] }
rusqlite = { version = "0.39", features = ["bundled"] }
anyhow = "1.0"
uuid = { version = "1", features = ["v4"] }
chrono = { version = "0.4", features = ["serde"] }
env_logger = "0.11"
dotenv = "0.15"
log = "0.4"
```

### Python (smart layer)

```
Python 3.11+
aiohttp          — async HTTP
asyncio          — async operations
sqlite3          — built-in, trade logging
python-dotenv    — API key management
loguru           — clean terminal logging
pydantic         — data validation
rapidfuzz        — fuzzy string matching for question matcher
pytest           — testing
```

### Tauri / SolidJS (UI layer)

```
Tauri 2          — native desktop shell
SolidJS          — reactive UI framework
TanStack Query   — data fetching with configurable polling
TypeScript       — type-safe frontend
```

---

## The Bridge (Rust ↔ Python)

Communication happens through a JSON pipe over stdin/stdout.

**Rust → Python (gap detected):**
```json
{
  "event": "gap_detected",
  "pair_type": "cross_platform",
  "market_id": "btc-above-95k-june-2026",
  "polymarket_price": 0.71,
  "kalshi_price": 0.58,
  "gap_cents": 13,
  "polymarket_token": "0xabc123",
  "kalshi_ticker": "FED-25JUN-T95",
  "timestamp": "2026-05-03T14:22:01Z"
}
```

**Python → Rust (execute order):**
```json
{
  "action": "execute",
  "polymarket_side": "NO",
  "polymarket_amount": 5.00,
  "kalshi_side": "YES",
  "kalshi_amount": 5.00,
  "gap_cents": 13.0,
  "dry_run": true,
  "taker_fee_rate": 0.02
}
```

**Rust → Python (confirmation):**
```json
{
  "event": "order_placed",
  "polymarket_order_id": "dry_a1b2c3d4",
  "kalshi_order_id": "dry_e5f6g7h8",
  "total_spent": 10.00,
  "expected_profit": 1.29,
  "dry_run": true
}
```

`expected_profit` is **net of fees** — `(payout - stake) - taker_fee_rate × stake`.

---

## Configuration

Copy `.env.example` to `.env` and fill in your values.

```bash
cp config/.env.example .env
```

**`.env.example`:**
```env
# --- MODE ---
DRY_RUN=true                        # true = simulate only, false = real money

# --- POLYMARKET ---
POLYMARKET_API_KEY=                 # from Polymarket dashboard
POLYMARKET_PRIVATE_KEY=             # your wallet private key
POLYMARKET_WALLET_ADDRESS=          # your wallet address
POLYMARKET_WS_URL=wss://ws-subscriptions.polymarket.com/ws/market
POLYMARKET_CLOB_URL=https://clob.polymarket.com
POLYMARKET_GAMMA_URL=https://gamma-api.polymarket.com

# --- KALSHI ---
KALSHI_API_KEY=                     # from Kalshi dashboard
KALSHI_API_SECRET=                  # from Kalshi dashboard
KALSHI_WS_URL=wss://trading-api.kalshi.com/trade-api/ws/v2
KALSHI_API_URL=https://trading-api.kalshi.com/trade-api/v2

# --- GAP THRESHOLDS ---
MIN_GAP_CENTS=5                     # ignore gaps smaller than this
MAX_GAP_CENTS=30                    # ignore gaps larger than this (likely data error)
EV_MIN_CENTS=1.0                    # minimum net EV in cents to execute
EV_TAKER_FEE_RATE=0.02              # 2% taker fee applied to total stake

# --- KELLY SIZING ---
BANKROLL_USDC=500.0                 # total capital across both platforms
MIN_BET_USDC=10                     # minimum bet size per trade
MAX_BET_USDC=100                    # maximum bet size per trade
KELLY_FRACTION=0.25                 # fractional Kelly multiplier (0.25 = quarter-Kelly)

# --- RISK ---
MAX_DAILY_LOSS_USDC=50              # stop trading if daily loss exceeds this
MAX_OPEN_POSITIONS=5                # maximum simultaneous open positions (0 = unlimited in dry-run)

# --- LOGGING ---
LOG_LEVEL=INFO                      # DEBUG, INFO, WARN, ERROR
DB_PATH=data/trades.db
```

---

## Gap Detection — 7-Layer Validation

`detector.py` validates every gap Rust sends before executing. All 7 layers must pass:

1. **Confidence filter** — reject `low` confidence pairs
2. **Gap range** — gap must be between `MIN_GAP_CENTS` and `MAX_GAP_CENTS`
3. **negRisk safety** — skip multi-outcome Polymarket markets (negRisk flag)
4. **Stability** — gap must appear in at least 2 consecutive price updates (not a spike)
5. **EV gate** — net EV after 2% fee must exceed `EV_MIN_CENTS`
6. **Daily loss limit** — stop if losses exceed `MAX_DAILY_LOSS_USDC`
7. **Open position cap** — skip if at `MAX_OPEN_POSITIONS` (unlimited in dry-run)

---

## Kelly Sizing

`kelly_engine.py` computes bet size using fractional Kelly criterion for two-leg arbitrage:

```
combined = price_A + price_B   (total stake for $1 payout)
b = (1 - combined) / combined  (odds)
p = execution_probability      (0.85 for medium confidence)
f* = (b×p - (1-p)) / b        (full Kelly fraction)
f  = min(f* × 0.25, 0.05)     (quarter-Kelly, capped at 5% of bankroll)
bet = clamp(bankroll × f, MIN_BET_USDC, MAX_BET_USDC)
```

---

## Fee-Accurate P&L

All profit figures shown in the UI are **net of the 2% taker fee**:

```
gross_profit = payout - stake = (stake / combined) - stake
fee          = taker_fee_rate × stake
net_profit   = gross_profit - fee
```

Example for a 13¢ gap with $10 stake:
- Gross: `(10 / 0.87) - 10 = $1.49`
- Fee: `0.02 × 10 = $0.20`
- **Net: $1.29** ← what you see in the dashboard

The `SIMULATED P&L` figure in the dashboard is the sum of all net expected profits from today's dry-run trades. This is what you would actually make if those trades had executed at the quoted prices.

---

## Trade Reconciliation

`reconciler.py` polls the Polymarket Gamma API to resolve open trades. When a market resolves:

- Fetches the winning outcome for each open position
- Computes `actual_profit` based on which side won
- Updates trade status to `closed` (profit or loss)
- Exposes resolved trades to the calibration and analytics panels

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
    executed INTEGER DEFAULT 0
);

CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_id INTEGER REFERENCES gaps(id),
    polymarket_order_id TEXT,
    kalshi_order_id TEXT,
    polymarket_side TEXT,
    kalshi_side TEXT,
    amount_usdc REAL,
    expected_profit REAL,      -- NET of fees
    actual_profit REAL,
    status TEXT,               -- open, closed
    dry_run INTEGER,
    opened_at TEXT,
    resolved_at TEXT
);

CREATE TABLE market_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polymarket_slug TEXT,
    kalshi_ticker TEXT,
    confidence TEXT,
    match_method TEXT,         -- exact, fuzzy, manual
    times_traded INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    created_at TEXT,
    last_seen TEXT
);

CREATE TABLE bot_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
```

**Query your results:**
```bash
# Net P&L today
sqlite3 data/trades.db "SELECT ROUND(SUM(expected_profit),2) FROM trades WHERE opened_at >= date('now');"

# All trades today
sqlite3 data/trades.db "SELECT * FROM trades WHERE opened_at >= date('now') ORDER BY opened_at DESC;"

# Win rate by market
sqlite3 data/trades.db "SELECT market_id, COUNT(*) as trades, ROUND(SUM(actual_profit),2) as pnl FROM trades WHERE actual_profit IS NOT NULL GROUP BY market_id ORDER BY pnl DESC;"

# Dry run summary
sqlite3 data/trades.db "SELECT COUNT(*) as trades, ROUND(SUM(expected_profit),2) as net_pnl FROM trades WHERE dry_run=1;"
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

# Node.js (for Tauri UI)
brew install node
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
cd tauri-app && npm install && cd ..

# Copy config
cp config/.env.example .env
```

### Configure

1. Create a Polymarket account — Settings → API → Generate key + note wallet address
2. Create a Kalshi account — Settings → API Access → Generate key and secret
3. Fill in `.env` — keep `DRY_RUN=true` until you're confident

---

## Running

### Option A — Tauri Desktop App (recommended)

```bash
cd tauri-app
npm run tauri dev
```

Click **Start Bot** in the UI. The dashboard polls live data every 2–3 seconds.

### Option B — Terminal only

```bash
python python-core/main.py
```

```
[14:22:01] INFO  | Bot started. DRY_RUN=true. Bankroll: $500
[14:22:03] INFO  | WebSocket connected: Polymarket
[14:22:03] INFO  | WebSocket connected: Kalshi
[14:22:07] INFO  | 80,708 market pairs loaded
[14:22:15] GAP   | 106067::88860102-41022920 | Poly: 0.024 | Kalshi: 0.845 | Gap: 13.1¢
[14:22:15] VALID | EV net: 11.5¢ | Kelly: $10.00 | Executing (DRY RUN)
[14:22:15] TRADE | YES Poly $5 | YES Kalshi $5 | Fee $0.20 | Net: +$1.29
[14:22:15] LOG   | Trade #47 written to trades.db
```

---

## Running Tests

```bash
cd python-core
uv run pytest tests/ -v
# 59 tests, all passing
```

Test coverage:
- `test_matcher.py` — fuzzy matching, edge cases, manual overrides
- `test_detector.py` — 7-layer validation, EV gate, threshold checks
- `test_ev_engine.py` — fee calculation, net EV accuracy
- `test_tracker.py` — SQLite write/read, bot_state, live trade queries
- `test_reconciler.py` — trade resolution, Gamma API mock

---

## Going Live

When dry-run results look good (recommend 100+ trades with consistent positive net P&L):

1. Verify Polymarket wallet has USDC funded (Polygon network)
2. Verify Kalshi account has USD funded
3. Set `DRY_RUN=false` in `.env`
4. Lower `MAX_BET_USDC` to `10` for the first week
5. Set `MAX_OPEN_POSITIONS=3` to start conservatively
6. Watch the terminal for the first hour

**Do not go live until:**
- 100+ dry-run trades show positive net P&L
- You have manually verified at least 10 gap matches by checking both platforms
- High-confidence pairs are driving most of the volume

---

## Realistic Numbers

Gaps appear 10–40 times per day across active markets. Most are 5–10 cents net of fees. Rare ones hit 15+ cents.

With $500 bankroll ($250 each platform), betting $10 per gap:
- Conservative: $8–20 net profit per day in active markets
- The edge shrinks as more bots enter — question matching speed and accuracy is the moat

---

## Architecture Decisions

**Why stdin/stdout pipe instead of gRPC?**
Zero infrastructure overhead for local Mac deployment. Upgrade to gRPC for VPS.

**Why SQLite instead of Postgres?**
Zero-config, fast enough at this volume. Swap when moving to a server.

**Why not 100% Rust?**
Gap validation logic (EV gate, Kelly sizing, confidence scoring) changes frequently during tuning. Python lets you iterate without a compile step. Rust handles the latency-critical parts.

**Why not 100% Python?**
Running two WebSocket feeds and a comparison hot loop simultaneously — Python's GIL is a ceiling. Rust removes it.

**Why fractional Kelly and not fixed bet?**
Fixed bets ignore edge size. Kelly maximizes long-run growth rate. Quarter-Kelly (0.25×) keeps drawdowns manageable while capturing most of the edge.

---

## Known Limitations

- Kalshi is US-only (requires US bank/card).
- Polymarket requires USDC on Polygon.
- Fees in the EV calculation use 2% flat. Real Polymarket fees vary by order type and volume tier. Kalshi uses per-contract cents. The 2% default is conservative.
- Price quotes are the last traded price, not the ask price you'll actually fill at. Real slippage reduces profit further, especially on Kalshi's thinner order book.
- Question matching is never perfect. The 7-layer validator and confidence system mitigate mismatch risk, but manual review of top pairs is still recommended before going live.

---

## Changelog

### May 5, 2026

**Fee-accurate P&L** — `expected_profit` stored in the database is now net of the 2% taker fee. Previously it was gross. All historical records migrated. The `SIMULATED P&L` figure in the dashboard now reflects what you'd actually make.

**Tauri desktop UI**
- Analytics toggle: RiskPanel, CalibrationPanel, PortfolioPanel behind a single button
- Pulsing green dot on the bot status indicator when running
- Trades table expanded to show up to 500 rows
- KPI polling floors lowered to 2s (KPIs) / 3s (gaps + trades) for near-live updates

**Analytics accuracy** — all four analytics queries (calibration, portfolio, risk, daily P&L) now include dry-run trades and handle the `closed` status. Previously only live trades appeared in analytics panels.

**Trade reconciler** (`reconciler.py`) — async agent that polls the Polymarket Gamma API to resolve open positions and write `actual_profit` back to the database.

**Fractional Kelly sizing** (`kelly_engine.py`) — bet size is now computed from bankroll using quarter-Kelly criterion per trade, capped at `MIN_BET_USDC` / `MAX_BET_USDC`. Previously all bets were fixed-size.

**7-layer gap validator** — EV gate added as layer 5: gaps where net EV (after 2% fee) is below `EV_MIN_CENTS` are rejected before execution.

**No open-position cap in dry-run** — `MAX_OPEN_POSITIONS` only applies in live mode. Dry-run runs unlimited positions for simulation accuracy.

**Test suite** — 59 tests across 5 test files. `conftest.py` with shared fixtures and sys.path fix.

---

## License

MIT
