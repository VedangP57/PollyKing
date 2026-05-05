# Arbitrage Bot — `arb`

A hybrid Rust + Python prediction market arbitrage bot. Rust handles real-time price feeds and order execution. Python handles question matching, gap validation, trade logging, and decision logic.

Trades between Polymarket and Kalshi. Finds the same question priced differently on both platforms, bets both sides simultaneously, and locks in guaranteed profit when prices converge.

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
├── matcher.py     — links same questions across platforms
├── detector.py    — validates gaps, checks thresholds
├── executor.py    — calls Rust executor, manages dry run vs live
├── tracker.py     — logs every trade to SQLite
└── main.py        — entry point, runs everything
```

When Rust detects a gap, it writes a JSON event to stdout. Python reads it, validates it, decides whether to execute, then writes back. Rust places both orders simultaneously. Python logs the result.

---

## What Is Arbitrage

Same question, two platforms, different prices.

Example:
- Polymarket: "Will Fed cut rates in June?" — 71 cents (YES)
- Kalshi: Same question — 58 cents (YES)

You buy YES on Kalshi at 58 cents. You buy NO on Polymarket at 29 cents (1.00 - 0.71). Total spent: 87 cents. When the question resolves, one side pays $1.00. You made 13 cents. Guaranteed. No prediction needed.

The bot does this automatically, many times per day, across all matching markets.

---

## Project Structure

```
arbitrage-bot/
├── rust-core/                    — Rust workspace
│   ├── Cargo.toml
│   ├── src/
│   │   ├── main.rs               — entry point, spawns tasks
│   │   ├── fetcher/
│   │   │   ├── mod.rs
│   │   │   ├── polymarket.rs     — Polymarket WebSocket listener
│   │   │   └── kalshi.rs         — Kalshi WebSocket listener
│   │   ├── comparator.rs         — hot loop, gap detection
│   │   ├── executor.rs           — order placement (both platforms)
│   │   ├── bridge.rs             — stdin/stdout JSON pipe to Python
│   │   └── types.rs              — shared structs (Price, Gap, Order)
│   └── tests/
│       └── comparator_tests.rs
│
├── python-core/                  — Python workspace
│   ├── main.py                   — entry point, starts Rust subprocess
│   ├── matcher.py                — question matching across platforms
│   ├── detector.py               — gap validation, threshold checks
│   ├── executor.py               — dry run / live execution logic
│   ├── tracker.py                — SQLite trade logging
│   ├── notifier.py               — terminal logging via loguru
│   └── tests/
│       ├── test_matcher.py
│       ├── test_detector.py
│       └── test_tracker.py
│
├── config/
│   ├── .env.example              — all config variables with defaults
│   └── markets.json              — manually curated question match pairs
│
├── data/
│   └── trades.db                 — SQLite database (auto-created on first run)
│
├── scripts/
│   ├── setup.sh                  — install all dependencies
│   └── backfill_matches.py       — seed markets.json with known pairs
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
thiserror = "2.0"
chrono = { version = "0.4", features = ["serde"] }
futures-util = "0.3"
crossbeam-channel = "0.5"
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

---

## The Bridge (Rust ↔ Python)

Communication happens through a JSON pipe over stdin/stdout. No sockets, no queues, no extra infrastructure. Works natively on Mac.

**Rust → Python (gap detected):**
```json
{
  "event": "gap_detected",
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
  "polymarket_amount": 50.00,
  "kalshi_side": "YES",
  "kalshi_amount": 50.00,
  "dry_run": true
}
```

**Rust → Python (confirmation):**
```json
{
  "event": "order_placed",
  "polymarket_order_id": "ord_abc",
  "kalshi_order_id": "ord_xyz",
  "total_spent": 43.50,
  "expected_profit": 6.50,
  "dry_run": true
}
```

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
POLYMARKET_API_URL=https://clob.polymarket.com

# --- KALSHI ---
KALSHI_API_KEY=                     # from Kalshi dashboard
KALSHI_API_SECRET=                  # from Kalshi dashboard
KALSHI_WS_URL=wss://trading-api.kalshi.com/trade-api/ws/v2
KALSHI_API_URL=https://trading-api.kalshi.com/trade-api/v2

# --- GAP THRESHOLDS ---
MIN_GAP_CENTS=5                     # ignore gaps smaller than this
MAX_GAP_CENTS=30                    # ignore gaps larger than this (likely data error)
MIN_BET_USDC=10                     # minimum bet size per side
MAX_BET_USDC=100                    # maximum bet size per side

# --- RISK ---
MAX_DAILY_LOSS_USDC=50              # stop trading if daily loss exceeds this
MAX_OPEN_POSITIONS=5                # maximum simultaneous open positions

# --- LOGGING ---
LOG_LEVEL=INFO                      # DEBUG, INFO, WARN, ERROR
DB_PATH=data/trades.db
```

---

## Question Matching

The hardest engineering problem in this bot. Polymarket calls it "Will Fed cut rates in June 2026?" — Kalshi calls it "Fed rate cut — June meeting". Same question, different names.

### How it works

Three-layer matching system in `matcher.py`:

**Layer 1 — Exact slug match**
If both platforms use identical slugs or tickers, match immediately. Fast. Covers ~30% of markets.

**Layer 2 — Fuzzy string match**
Uses `rapidfuzz` to compare question titles after normalizing whitespace, punctuation, and common abbreviations. Threshold: 85% similarity. Covers ~50% of markets.

**Layer 3 — Manual overrides**
`config/markets.json` stores known pairs that fuzzy matching gets wrong. Human-curated. Grows over time as the bot learns.

```json
{
  "manual_pairs": [
    {
      "polymarket_slug": "will-fed-cut-rates-june-2026",
      "kalshi_ticker": "FED-25JUN",
      "confidence": "high",
      "notes": "Different date format"
    }
  ]
}
```

**Confidence scoring** (mirrors Rocket-Support brain.db pattern):
- `high` — exact or near-exact match, auto-execute when gap fires
- `medium` — fuzzy match above threshold, execute but log for review
- `low` — uncertain match, log only, never auto-execute

---

## Gap Detection

`detector.py` validates every gap Rust sends before deciding to execute.

**A gap is valid when:**
1. Combined price (YES side A + NO side B) is below $0.95 (leaving room for fees)
2. Gap has been stable for at least 3 consecutive price updates (not a data spike)
3. Both markets have sufficient liquidity (order book depth check)
4. Neither market is within 10 minutes of resolution (theta risk)
5. Daily loss limit has not been hit
6. Open position count is below maximum

**Gap is rejected when:**
- Gap appears and disappears in a single update (noise)
- One platform's WebSocket feed is lagging (stale data risk)
- Market confidence is `low`
- DRY_RUN is false and balance is insufficient

---

## Trade Logging

Every event is logged to `data/trades.db` via `tracker.py`. Same pattern as Rocket-Support's brain.db.

**Schema:**

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
    expected_profit REAL,
    actual_profit REAL,
    status TEXT,         -- open, resolved, cancelled
    dry_run INTEGER,
    opened_at TEXT,
    resolved_at TEXT
);

CREATE TABLE market_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polymarket_slug TEXT,
    kalshi_ticker TEXT,
    confidence TEXT,
    match_method TEXT,   -- exact, fuzzy, manual
    times_traded INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    created_at TEXT,
    last_seen TEXT
);
```

**Query your results:**
```bash
# All trades today
sqlite3 data/trades.db "SELECT * FROM trades WHERE opened_at > date('now') ORDER BY opened_at DESC;"

# Win rate by market
sqlite3 data/trades.db "SELECT market_id, COUNT(*) as trades, SUM(actual_profit) as total_profit FROM trades GROUP BY market_id ORDER BY total_profit DESC;"

# Dry run summary
sqlite3 data/trades.db "SELECT COUNT(*) as trades, SUM(expected_profit) as simulated_profit FROM trades WHERE dry_run=1;"
```

---

## Setup

### Prerequisites

```bash
# Rust (you likely have this)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Python 3.11+
brew install python@3.11

# uv (fast Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Install

```bash
git clone https://github.com/yourusername/arbitrage-bot.git
cd arbitrage-bot

# Install Python dependencies
cd python-core
uv sync
cd ..

# Build Rust
cd rust-core
cargo build --release
cd ..

# Copy config
cp config/.env.example .env
```

### Configure

1. Create a Polymarket account at polymarket.com
   - Go to Settings → API
   - Generate API key
   - Note your wallet address and export private key

2. Create a Kalshi account at kalshi.com
   - Go to Settings → API Access
   - Generate API key and secret

3. Fill in your `.env` file — keep `DRY_RUN=true` until you're confident

### Seed market pairs

```bash
# Downloads all active markets from both platforms and runs the matcher
python scripts/backfill_matches.py
```

This populates `markets.json` with known pairs and seeds `trades.db` with the `market_pairs` table. Run once before first launch.

---

## Running

```bash
# Start the bot (dry run by default)
python python-core/main.py

# What you'll see in terminal:
# [14:22:01] INFO  | Bot started. DRY_RUN=true
# [14:22:03] INFO  | WebSocket connected: Polymarket
# [14:22:03] INFO  | WebSocket connected: Kalshi
# [14:22:07] INFO  | 847 market pairs loaded (312 high confidence, 421 medium, 114 low)
# [14:22:15] GAP   | btc-above-95k-june | Poly: 0.71 | Kalshi: 0.58 | Gap: 13c | Confidence: HIGH
# [14:22:15] VALID | Gap stable for 3 updates. Liquidity OK. Executing (DRY RUN)
# [14:22:15] TRADE | YES Kalshi $50 @ 0.58 | NO Poly $50 @ 0.29 | Expected: +$6.50
# [14:22:15] LOG   | Trade #47 written to trades.db
```

---

## Going Live

When dry run results look good (recommend 100+ simulated trades with positive expected profit):

1. Verify your Polymarket wallet has USDC funded
2. Verify your Kalshi account has USD funded
3. In `.env`, set `DRY_RUN=false`
4. Lower `MAX_BET_USDC` to $10-20 for first week of live trading
5. Watch the terminal closely for the first hour

**Do not go live until:**
- Dry run shows consistent positive expected profit across 100+ gaps
- Question matching confidence is high on the gaps firing most frequently
- You have manually verified at least 10 matches by checking both platforms yourself

---

## Testing

```bash
# Python tests
cd python-core
uv run pytest tests/ -v

# Rust tests
cd rust-core
cargo test

# Integration test (runs full pipeline with mock WebSocket data)
python scripts/integration_test.py
```

**Test coverage:**
- `test_matcher.py` — fuzzy matching accuracy, edge cases, manual overrides
- `test_detector.py` — gap validation logic, threshold checks, stale data detection
- `test_tracker.py` — SQLite write/read, query accuracy
- `comparator_tests.rs` — gap math, price normalization, edge cases

---

## Realistic Numbers

Gaps appear 15-40 times per day across active markets. Most are 3-8 cents. Some are 10-15 cents. Rare ones hit 20+ cents (usually data errors — the validator catches these).

With $500 split across both platforms ($250 each), betting $10-20 per gap:
- Conservative estimate: $15-40 profit per day in active markets
- This compounds as you increase bet size and confidence in your matches

The edge shrinks over time as more bots enter. The bot with the best question matcher and fastest execution wins.

---

## Architecture Decisions

**Why stdin/stdout pipe instead of gRPC or sockets?**
Simplest possible bridge for a local Mac setup. Zero extra infrastructure. Easy to debug — you can read the pipe output directly. Upgrade to gRPC when deploying to a VPS.

**Why SQLite instead of Postgres?**
Running locally. SQLite is zero-config and fast enough for this volume. Swap to Postgres when moving to a server.

**Why not 100% Rust?**
Question matching requires flexible string logic that changes frequently as you tune thresholds and add manual overrides. Python lets you iterate fast. Rust handles the parts where speed actually matters — WebSocket feeds and order execution.

**Why not 100% Python?**
WebSocket feeds from two platforms simultaneously, running a comparison loop thousands of times per second — Python's GIL would be a real bottleneck here. Rust removes that ceiling.

---

## Known Limitations

- Kalshi is US-only. Requires US bank account or card to fund.
- Polymarket requires a crypto wallet (USDC on Polygon network).
- Question matching is never perfect. Some gaps will fire on mismatched questions. The confidence system and manual review process mitigate this.
- Both platforms have rate limits on their APIs. The bot respects these but aggressive settings can trigger them.
- Fees reduce actual profit. Polymarket charges ~2% maker/taker. Kalshi charges ~7 cents per contract. Always verify fee-adjusted profit before sizing up.

---

## Roadmap

- [ ] Add Manifold as third platform
- [ ] gRPC bridge for VPS deployment
- [ ] Web dashboard for trade history (replace sqlite3 CLI queries)
- [ ] Automatic question match learning — when a manually verified pair resolves correctly, auto-promote confidence
- [ ] Fee-adjusted gap detection — incorporate real fee data from both APIs into gap validation

---

## License

MIT
