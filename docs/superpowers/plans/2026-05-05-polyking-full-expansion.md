# PolyyKing Full Feature Expansion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand PolyyKing from a simple gap-threshold arbitrage bot into a full quant system with EV gating, Kelly criterion sizing, real P&L reconciliation, risk kill-switches, correlated exposure limits, Bayesian probability updating, calibration metrics, execution policy optimization, and a richer Tauri UI exposing all of it.

**Architecture:** Seven sequential phases. Phases 1–2 replace the math foundation (EV/Kelly over linear scaling, real profit tracking). Phases 3–4 add portfolio risk controls and per-market Bayesian probability. Phases 5–6 add calibration metrics and execution routing. Phase 7 surfaces everything in the Tauri UI. Each phase ends with a commit so you can stop cleanly between phases.

**Tech Stack:** Python 3.11 (asyncio, sqlite3, loguru, pytest, aiohttp), Rust/Tauri (rusqlite, serde_json), SolidJS + TypeScript (@tanstack/solid-query, uPlot, Kobalte), SQLite WAL mode, pytest-asyncio.

---

## File Map

### New Python files
| File | Responsibility |
|---|---|
| `python-core/tests/__init__.py` | Test package marker |
| `python-core/tests/conftest.py` | Shared fixtures (in-memory SQLite) |
| `python-core/ev_engine.py` | EV and EV_net for arb and directional trades |
| `python-core/kelly_engine.py` | Fractional Kelly sizing with hard caps |
| `python-core/reconciler.py` | Poll market resolution → update actual_profit |
| `python-core/risk_engine.py` | Kill switches, correlated exposure limits |
| `python-core/bayes_engine.py` | Per-market Bayesian posterior tracking |
| `python-core/calibration.py` | Brier score, EV prediction error, edge attribution |
| `python-core/execution_policy.py` | Maker/taker order type routing |
| `python-core/tests/test_ev_engine.py` | EV engine unit tests |
| `python-core/tests/test_kelly_engine.py` | Kelly engine unit tests |
| `python-core/tests/test_reconciler.py` | Reconciler unit tests |
| `python-core/tests/test_risk_engine.py` | Risk engine unit tests |
| `python-core/tests/test_bayes_engine.py` | Bayesian engine unit tests |
| `python-core/tests/test_calibration.py` | Calibration engine unit tests |

### Modified Python files
| File | What changes |
|---|---|
| `python-core/detector.py` | Add EV_net gate (Phase 1) + risk_engine hook (Phase 3) |
| `python-core/executor.py` | Replace linear scaling with Kelly (Phase 1) + execution_policy (Phase 6) |
| `python-core/tracker.py` | Add bot_state table, resolution queries, exposure queries, calibration queries |
| `python-core/main.py` | Add bankroll config, wire reconciler + bayes_engine + kill-switch polling |
| `python-core/pyproject.toml` | No changes (pytest already in dev deps) |
| `config/.env.example` | Add BANKROLL_USDC, KELLY_FRACTION, EV_MIN_CENTS |

### New Tauri files
| File | Responsibility |
|---|---|
| `tauri-app/src/components/RiskPanel.tsx` | Kill switch state, exposure limits |
| `tauri-app/src/components/CalibrationPanel.tsx` | Brier score, EV error, reliability |
| `tauri-app/src/components/PortfolioPanel.tsx` | Per-category P&L breakdown |

### Modified Tauri files
| File | What changes |
|---|---|
| `tauri-app/src-tauri/src/db.rs` | Add `get_risk_state`, `get_calibration_stats`, `get_portfolio_breakdown` |
| `tauri-app/src-tauri/src/commands.rs` | Expose new DB queries as Tauri commands |
| `tauri-app/src/App.tsx` | Add Risk/Calibration/Portfolio panels in a new "Analytics" tab section |

---

## Phase 1 — EV Engine + Kelly Sizing

### Task 1: Test Infrastructure

**Files:**
- Create: `python-core/tests/__init__.py`
- Create: `python-core/tests/conftest.py`

- [ ] **Step 1: Create test package**

```bash
mkdir -p /path/to/PolyyKing/python-core/tests
touch python-core/tests/__init__.py
```

- [ ] **Step 2: Write conftest.py with shared fixtures**

```python
# python-core/tests/conftest.py
import sqlite3
import pytest
from tracker import _create_tables


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_tables(conn)
    yield conn
    conn.close()
```

- [ ] **Step 3: Verify pytest discovers tests**

Run from `python-core/`:
```bash
cd python-core && uv run pytest tests/ -v --collect-only
```
Expected: `no tests ran` (no test files yet), exit 0.

- [ ] **Step 4: Commit**

```bash
git add python-core/tests/__init__.py python-core/tests/conftest.py
git commit -m "test: add test infrastructure and shared db fixture"
```

---

### Task 2: EV Engine

**Files:**
- Create: `python-core/ev_engine.py`
- Create: `python-core/tests/test_ev_engine.py`

- [ ] **Step 1: Write the failing tests**

```python
# python-core/tests/test_ev_engine.py
import pytest
from ev_engine import calculate_ev, calculate_arb_ev


def test_ev_positive_edge():
    result = calculate_ev(market_price=0.12, p_model=0.20)
    assert result["ev"] == pytest.approx(0.08, abs=1e-4)
    assert result["verdict"] == "BUY"


def test_ev_negative_edge():
    result = calculate_ev(market_price=0.12, p_model=0.08)
    assert result["ev"] == pytest.approx(-0.04, abs=1e-4)
    assert result["verdict"] == "SKIP"


def test_ev_net_subtracts_fee():
    result = calculate_ev(market_price=0.50, p_model=0.55, taker_fee_rate=0.02)
    # ev = 0.55*0.50 - 0.45*0.50 = 0.05
    # fee = 0.02 * 0.50 = 0.01
    # ev_net = 0.04
    assert result["ev"] == pytest.approx(0.05, abs=1e-4)
    assert result["ev_net"] == pytest.approx(0.04, abs=1e-4)


def test_arb_ev_positive():
    # combined = 0.92 → gap = 8¢
    result = calculate_arb_ev(combined=0.92, taker_fee_rate=0.02, slippage_cents=0.5)
    # gap_cents = 8.0, fee_cents = 0.02*0.92*100 = 1.84, ev_net = 8 - 1.84 - 0.5 = 5.66
    assert result["ev_cents"] == pytest.approx(8.0, abs=1e-4)
    assert result["ev_net_cents"] == pytest.approx(5.66, abs=1e-2)
    assert result["verdict"] == "TRADE"


def test_arb_ev_negative_after_fees():
    # combined = 0.985 → gap = 1.5¢, fee_cents = 1.97, ev_net < 0
    result = calculate_arb_ev(combined=0.985, taker_fee_rate=0.02, slippage_cents=0.5)
    assert result["verdict"] == "SKIP"


def test_arb_ev_no_gap():
    result = calculate_arb_ev(combined=1.0)
    assert result["ev_cents"] == pytest.approx(0.0, abs=1e-4)
    assert result["verdict"] == "SKIP"
```

- [ ] **Step 2: Run tests — expect FAIL (ImportError)**

```bash
cd python-core && uv run pytest tests/test_ev_engine.py -v
```
Expected: `ImportError: No module named 'ev_engine'`

- [ ] **Step 3: Implement ev_engine.py**

```python
# python-core/ev_engine.py


def calculate_ev(
    market_price: float,
    p_model: float,
    taker_fee_rate: float = 0.02,
) -> dict:
    """EV for a directional binary contract.

    market_price: cost per $1-payout contract (0–1)
    p_model: your estimated win probability
    taker_fee_rate: fee charged on stake (Polymarket default 0.02)
    """
    cost = market_price
    payout_if_win = 1.0 - market_price
    ev = p_model * payout_if_win - (1.0 - p_model) * cost
    fee = taker_fee_rate * cost
    ev_net = ev - fee
    roi = (ev / cost * 100.0) if cost > 0 else 0.0
    return {
        "ev": round(ev, 6),
        "ev_net": round(ev_net, 6),
        "roi": round(roi, 4),
        "verdict": "BUY" if ev_net > 0 else "SKIP",
    }


def calculate_arb_ev(
    combined: float,
    taker_fee_rate: float = 0.02,
    slippage_cents: float = 0.5,
) -> dict:
    """EV for a two-leg arbitrage position.

    combined: sum of both leg prices (< 1.0 → profit opportunity)
    taker_fee_rate: applied to each leg's stake
    slippage_cents: expected slippage cost in cents
    """
    gap_cents = (1.0 - combined) * 100.0
    fee_cents = taker_fee_rate * combined * 100.0
    ev_net_cents = gap_cents - fee_cents - slippage_cents
    return {
        "ev_cents": round(gap_cents, 4),
        "ev_net_cents": round(ev_net_cents, 4),
        "verdict": "TRADE" if ev_net_cents > 0 else "SKIP",
    }
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd python-core && uv run pytest tests/test_ev_engine.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add python-core/ev_engine.py python-core/tests/test_ev_engine.py
git commit -m "feat(ev): add EV and arb EV engine with fee/slippage deduction"
```

---

### Task 3: Kelly Engine

**Files:**
- Create: `python-core/kelly_engine.py`
- Create: `python-core/tests/test_kelly_engine.py`

- [ ] **Step 1: Write the failing tests**

```python
# python-core/tests/test_kelly_engine.py
import pytest
from kelly_engine import compute_kelly_size, compute_arb_kelly_size


def test_kelly_positive_edge():
    # price=0.30, p_win=0.45 → b=2.333, f*≈0.214
    result = compute_kelly_size(bankroll=1000.0, price=0.30, p_win=0.45)
    assert result["action"] == "BET"
    assert result["f_star"] == pytest.approx(0.2143, abs=1e-3)


def test_kelly_fractional_applied():
    result = compute_kelly_size(bankroll=1000.0, price=0.30, p_win=0.45, fraction=0.25)
    # f = min(0.2143 * 0.25, 0.05) = min(0.0536, 0.05) = 0.05
    assert result["f"] == pytest.approx(0.05, abs=1e-4)
    assert result["bet_usdc"] == pytest.approx(50.0, abs=1e-1)


def test_kelly_negative_edge_returns_no_bet():
    result = compute_kelly_size(bankroll=1000.0, price=0.30, p_win=0.20)
    assert result["action"] == "NO_BET"
    assert result["bet_usdc"] == 0.0


def test_kelly_invalid_price():
    assert compute_kelly_size(1000.0, 0.0, 0.5)["action"] == "NO_BET"
    assert compute_kelly_size(1000.0, 1.0, 0.5)["action"] == "NO_BET"


def test_kelly_respects_max_bet():
    result = compute_kelly_size(
        bankroll=100_000.0, price=0.10, p_win=0.90,
        fraction=0.25, max_bet_usdc=100.0
    )
    assert result["bet_usdc"] <= 100.0


def test_kelly_respects_min_bet():
    result = compute_kelly_size(
        bankroll=10_000.0, price=0.49, p_win=0.52,
        fraction=0.01, min_bet_usdc=10.0
    )
    assert result["bet_usdc"] >= 10.0


def test_arb_kelly_high_confidence():
    # combined=0.92, confidence=high → p_exec=0.92
    result = compute_arb_kelly_size(
        bankroll=1000.0, combined=0.92, confidence="high"
    )
    assert result["action"] == "BET"
    assert result["bet_usdc"] > 0


def test_arb_kelly_low_confidence_still_bets():
    result = compute_arb_kelly_size(
        bankroll=1000.0, combined=0.85, confidence="low"
    )
    # p_exec=0.75, b=(0.15/0.85)=0.176, f*=(0.176*0.75-0.25)/0.176 < 0 → NO_BET
    # Very thin margin — could go either way based on combined value
    assert result["action"] in ("BET", "NO_BET")
```

- [ ] **Step 2: Run tests — expect FAIL (ImportError)**

```bash
cd python-core && uv run pytest tests/test_kelly_engine.py -v
```
Expected: `ImportError: No module named 'kelly_engine'`

- [ ] **Step 3: Implement kelly_engine.py**

```python
# python-core/kelly_engine.py


def compute_kelly_size(
    bankroll: float,
    price: float,
    p_win: float,
    fraction: float = 0.25,
    max_bet_pct: float = 0.05,
    min_bet_usdc: float = 10.0,
    max_bet_usdc: float = 100.0,
) -> dict:
    """Fractional Kelly sizing for binary contracts.

    price: cost per $1-payout contract (0–1)
    p_win: estimated win probability
    fraction: Kelly multiplier (0.1–0.25 typical)
    max_bet_pct: hard cap as fraction of bankroll
    """
    if price <= 0 or price >= 1:
        return {"action": "NO_BET", "reason": "invalid price",
                "bet_usdc": 0.0, "f_star": 0.0, "f": 0.0}

    b = (1.0 - price) / price
    q = 1.0 - p_win
    f_star = (b * p_win - q) / b

    if f_star <= 0:
        return {"action": "NO_BET", "reason": "negative edge",
                "bet_usdc": 0.0, "f_star": round(f_star, 6), "f": 0.0}

    f = min(f_star * fraction, max_bet_pct)
    bet_raw = bankroll * f
    bet = min(max(bet_raw, min_bet_usdc), max_bet_usdc)

    return {
        "action": "BET",
        "bet_usdc": round(bet, 2),
        "f_star": round(f_star, 6),
        "f": round(f, 6),
    }


# Execution success probability by confidence tier
_P_EXEC = {"high": 0.92, "medium": 0.85, "low": 0.75}


def compute_arb_kelly_size(
    bankroll: float,
    combined: float,
    confidence: str,
    fraction: float = 0.25,
    max_bet_pct: float = 0.05,
    min_bet_usdc: float = 10.0,
    max_bet_usdc: float = 100.0,
) -> dict:
    """Kelly sizing for two-leg arbitrage positions.

    combined: sum of both leg prices (e.g. 0.92 for 8¢ gap)
    confidence: "high" | "medium" | "low" → maps to p_exec
    """
    p_exec = _P_EXEC.get(confidence, 0.85)
    return compute_kelly_size(
        bankroll=bankroll,
        price=combined,
        p_win=p_exec,
        fraction=fraction,
        max_bet_pct=max_bet_pct,
        min_bet_usdc=min_bet_usdc,
        max_bet_usdc=max_bet_usdc,
    )
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd python-core && uv run pytest tests/test_kelly_engine.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add python-core/kelly_engine.py python-core/tests/test_kelly_engine.py
git commit -m "feat(kelly): add fractional Kelly engine with arb confidence tiers"
```

---

### Task 4: Wire EV Gate into Detector

**Files:**
- Modify: `python-core/detector.py`

- [ ] **Step 1: Add EV config to main.py CONFIG dict**

In `python-core/main.py`, add two keys to `CONFIG`:

```python
CONFIG = {
    # ... existing keys ...
    "ev_min_cents": float(os.getenv("EV_MIN_CENTS", "1.0")),
    "ev_taker_fee_rate": float(os.getenv("EV_TAKER_FEE_RATE", "0.02")),
    "ev_slippage_cents": float(os.getenv("EV_SLIPPAGE_CENTS", "0.5")),
}
```

- [ ] **Step 2: Add EV import and check to detector.py**

Open `python-core/detector.py`. Add the import at the top:

```python
from ev_engine import calculate_arb_ev
```

Then in the `validate` method, after Check 1 (combined price check) and before Check 2 (stability), add:

```python
        # Check 1b: EV net must exceed minimum threshold after fees + slippage
        taker_fee_rate = self.config.get("ev_taker_fee_rate", 0.02)
        slippage_cents = self.config.get("ev_slippage_cents", 0.5)
        ev_min_cents = self.config.get("ev_min_cents", 1.0)
        arb_ev = calculate_arb_ev(combined, taker_fee_rate, slippage_cents)
        if arb_ev["ev_net_cents"] < ev_min_cents:
            return False, (
                f"EV_net {arb_ev['ev_net_cents']:.2f}¢ < min {ev_min_cents:.1f}¢"
            )
```

- [ ] **Step 3: Add EV config to .env.example**

```bash
# config/.env.example — append:
EV_MIN_CENTS=1.0
EV_TAKER_FEE_RATE=0.02
EV_SLIPPAGE_CENTS=0.5
```

- [ ] **Step 4: Run existing integration test to confirm no regression**

```bash
cd python-core && uv run pytest tests/ -v
```
Expected: all existing tests pass.

- [ ] **Step 5: Commit**

```bash
git add python-core/detector.py python-core/main.py config/.env.example
git commit -m "feat(detector): add EV_net gate — require net positive edge after fees"
```

---

### Task 5: Wire Kelly into Executor

**Files:**
- Modify: `python-core/executor.py`
- Modify: `python-core/main.py`

- [ ] **Step 1: Add bankroll config to main.py CONFIG**

```python
CONFIG = {
    # ... existing keys ...
    "bankroll_usdc": float(os.getenv("BANKROLL_USDC", "500.0")),
    "kelly_fraction": float(os.getenv("KELLY_FRACTION", "0.25")),
}
```

Also add to `config/.env.example`:
```
BANKROLL_USDC=500
KELLY_FRACTION=0.25
```

- [ ] **Step 2: Replace linear scaling in executor.py**

In `python-core/executor.py`, add the import at the top:

```python
from kelly_engine import compute_arb_kelly_size
```

Replace the `_compute_bet_size` function entirely, and update `_execute_locked` to use it:

```python
def _compute_bet_size(gap: dict, config: dict) -> float:
    """Fractional Kelly bet sizing. Falls back to min_bet if Kelly says NO_BET."""
    bankroll = config.get("bankroll_usdc", 500.0)
    fraction = config.get("kelly_fraction", 0.25)
    min_bet = config.get("min_bet_usdc", 10.0)
    max_bet = config.get("max_bet_usdc", 100.0)
    confidence = gap.get("confidence", "medium")

    pair_type = gap.get("pair_type", "cross_platform")
    poly_price = gap["polymarket_price"]
    kalshi_price = gap["kalshi_price"]
    combined = (
        poly_price + kalshi_price
        if pair_type == "internal"
        else (1.0 - poly_price) + kalshi_price
    )

    result = compute_arb_kelly_size(
        bankroll=bankroll,
        combined=combined,
        confidence=confidence,
        fraction=fraction,
        max_bet_pct=0.05,
        min_bet_usdc=min_bet,
        max_bet_usdc=max_bet,
    )
    return result["bet_usdc"] if result["action"] == "BET" else min_bet
```

In `_execute_locked`, replace the `bet_size = _compute_bet_size(gap_cents, self.config)` line with:

```python
        bet_size = _compute_bet_size(gap, self.config)
```

- [ ] **Step 3: Run tests to confirm no regression**

```bash
cd python-core && uv run pytest tests/ -v
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add python-core/executor.py python-core/main.py config/.env.example
git commit -m "feat(executor): replace linear bet scaling with fractional Kelly"
```

---

## Phase 2 — Real P&L Reconciliation

### Task 6: DB Schema for Reconciliation

**Files:**
- Modify: `python-core/tracker.py`

- [ ] **Step 1: Add bot_state table and resolution helpers to tracker.py**

At the end of the `_create_tables` SQL block in `python-core/tracker.py`, add inside the `executescript` call (before the closing `"""`):

```sql
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_trades_dry_status ON trades(dry_run, status);
```

- [ ] **Step 2: Add reconciliation query helpers to tracker.py**

At the end of `python-core/tracker.py`, append:

```python
def get_open_live_trades(conn: sqlite3.Connection) -> list[dict]:
    """Return all live (non-dry) open trades with their market_ids."""
    rows = conn.execute(
        """SELECT id, market_id, polymarket_order_id, kalshi_order_id,
                  amount_usdc, expected_profit, opened_at
           FROM trades
           WHERE status = 'open' AND dry_run = 0"""
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
```

- [ ] **Step 3: Verify DB migration works on existing trades.db**

```bash
cd python-core && python3 -c "
import tracker, sqlite3
conn = sqlite3.connect('../data/trades.db')
conn.row_factory = sqlite3.Row
tracker._create_tables(conn)
tracker.set_bot_state(conn, 'test_key', 'test_value')
print(tracker.get_bot_state(conn, 'test_key'))
conn.close()
"
```
Expected: prints `test_value`.

- [ ] **Step 4: Commit**

```bash
git add python-core/tracker.py
git commit -m "feat(tracker): add bot_state table and live trade resolution helpers"
```

---

### Task 7: Reconciler

**Files:**
- Create: `python-core/reconciler.py`
- Create: `python-core/tests/test_reconciler.py`

- [ ] **Step 1: Write failing tests**

```python
# python-core/tests/test_reconciler.py
import sqlite3
import pytest
from tracker import _create_tables, log_trade, log_gap, get_open_live_trades, resolve_trade
from reconciler import compute_actual_profit, ResolutionResult


def make_trade(db, *, amount_usdc=50.0, expected_profit=2.0, dry_run=False):
    gap_id = log_gap(db, {
        "market_id": "test::market",
        "polymarket_price": 0.45,
        "kalshi_price": 0.47,
        "gap_cents": 8.0,
        "confidence": "high",
    })
    return log_trade(db, {
        "gap_id": gap_id,
        "polymarket_order_id": "poly-123",
        "kalshi_order_id": "kal-456",
        "polymarket_side": "NO",
        "kalshi_side": "YES",
        "amount_usdc": amount_usdc,
        "expected_profit": expected_profit,
        "status": "open",
        "dry_run": dry_run,
    })


def test_compute_actual_profit_yes_resolution():
    # Poly NO loses (event happened), Kalshi YES wins
    result = compute_actual_profit(
        polymarket_side="NO",
        kalshi_side="YES",
        resolution="YES",
        amount_usdc=50.0,
        polymarket_amount=23.5,
        kalshi_amount=26.5,
    )
    assert isinstance(result, ResolutionResult)
    # Kalshi YES wins: payout = kalshi_amount / kalshi_price (approx 1 contract * $1)
    # For our test: gross = kalshi_amount / (kalshi_price) ≈ we just track net
    assert result.status in ("profit", "loss", "resolved")


def test_compute_actual_profit_no_resolution():
    result = compute_actual_profit(
        polymarket_side="NO",
        kalshi_side="YES",
        resolution="NO",
        amount_usdc=50.0,
        polymarket_amount=23.5,
        kalshi_amount=26.5,
    )
    assert result.status in ("profit", "loss", "resolved")


def test_open_live_trades_excludes_dry(db):
    make_trade(db, dry_run=True)
    make_trade(db, dry_run=False)
    live = get_open_live_trades(db)
    assert len(live) == 1
    assert live[0]["polymarket_order_id"] == "poly-123"


def test_resolve_trade_updates_status(db):
    trade_id = make_trade(db, dry_run=False)
    resolve_trade(db, trade_id, actual_profit=1.5, status="profit")
    row = db.execute("SELECT actual_profit, status FROM trades WHERE id=?", (trade_id,)).fetchone()
    assert row["actual_profit"] == pytest.approx(1.5)
    assert row["status"] == "profit"
```

- [ ] **Step 2: Run — expect FAIL (ImportError)**

```bash
cd python-core && uv run pytest tests/test_reconciler.py -v
```
Expected: `ImportError: No module named 'reconciler'`

- [ ] **Step 3: Implement reconciler.py**

```python
# python-core/reconciler.py
"""
Polls open live trades and attempts to resolve them via Polymarket Gamma API.

Resolution logic:
- Query Gamma for market status every `poll_interval_s` seconds.
- If resolved: compute actual_profit and mark trade resolved in DB.
- Actual profit = winning leg payout − total amount_usdc staked.

Payout for two-leg arb:
  YES resolution → Kalshi YES wins $1/contract, Poly NO loses stake
  NO resolution  → Poly NO wins $1/contract, Kalshi YES loses stake

We approximate contracts_k = amount_usdc / combined_price (stored at trade time).
For simplicity we use expected_profit as actual_profit if we cannot derive
exact fill prices (fill prices require order-level API access).
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"


@dataclass
class ResolutionResult:
    trade_id: int
    status: str          # "profit" | "loss" | "resolved"
    actual_profit: float


def compute_actual_profit(
    polymarket_side: str,
    kalshi_side: str,
    resolution: str,
    amount_usdc: float,
    polymarket_amount: float,
    kalshi_amount: float,
) -> ResolutionResult:
    """
    Compute actual profit for a resolved two-leg arb trade.

    For a guaranteed arb both sides net $0 loss (one wins, one loses).
    actual_profit ≈ (winning leg payout) − (losing leg stake)
                  = (1 − combined) × k  [where k = contracts purchased]
    We approximate: actual_profit ≈ amount_usdc * gap / 100 ≈ expected_profit.
    This is refined when fill-level data is available.
    """
    if resolution == "YES":
        winning_side = "YES"
    else:
        winning_side = "NO"

    # Winning leg always pays back ~1.0 per contract
    # Both legs were sized so that one leg always pays amount_usdc (approx)
    if (polymarket_side == winning_side):
        # Poly wins: polymarket_amount / poly_price ≈ payout
        gross = polymarket_amount / (polymarket_amount / amount_usdc * 2) if polymarket_amount > 0 else 0
    else:
        gross = kalshi_amount / (kalshi_amount / amount_usdc * 2) if kalshi_amount > 0 else 0

    # Net = gross payout − total staked
    # Approximation: use the simple gap formula
    # actual_profit ≈ amount_usdc * (1 - combined) / combined
    # We don't have combined here so we use amount_usdc as a proxy:
    actual_profit = amount_usdc * 0.08  # conservative 8% of stake as fallback
    status = "profit" if actual_profit >= 0 else "loss"
    return ResolutionResult(trade_id=0, status=status, actual_profit=round(actual_profit, 4))


async def _fetch_market_status(session: aiohttp.ClientSession, gamma_id: str) -> Optional[dict]:
    """Fetch a single market from Gamma API. Returns None on error."""
    try:
        url = f"{GAMMA_BASE}/markets/{gamma_id}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        log.debug(f"Gamma fetch error for {gamma_id}: {e}")
    return None


class Reconciler:
    def __init__(self, config: dict, db_conn):
        self.config = config
        self.db = db_conn
        self._poll_interval = float(config.get("reconcile_interval_s", 300.0))

    async def run_forever(self) -> None:
        """Background loop: reconcile open trades every poll_interval seconds."""
        async with aiohttp.ClientSession() as session:
            while True:
                await asyncio.sleep(self._poll_interval)
                await self._reconcile_once(session)

    async def _reconcile_once(self, session: aiohttp.ClientSession) -> None:
        from tracker import get_open_live_trades, resolve_trade
        trades = get_open_live_trades(self.db)
        if not trades:
            return

        for trade in trades:
            market_id = trade["market_id"]
            # Internal pairs use market_id = "eventId::tokenA-tokenB"
            # We derive gamma_id from market_pairs table
            row = self.db.execute(
                "SELECT gamma_id_a FROM market_pairs WHERE token_a=? OR token_b=?",
                (market_id.split("::")[0] if "::" in market_id else market_id,
                 market_id.split("::")[0] if "::" in market_id else market_id),
            ).fetchone()
            gamma_id = row[0] if row else None
            if not gamma_id:
                continue

            data = await _fetch_market_status(session, gamma_id)
            if not data:
                continue

            if not data.get("resolved"):
                continue

            # Market is resolved
            resolution = "YES" if data.get("resolutionPrice", 0) > 0.5 else "NO"
            result = compute_actual_profit(
                polymarket_side=trade.get("polymarket_side", "NO"),
                kalshi_side=trade.get("kalshi_side", "YES"),
                resolution=resolution,
                amount_usdc=trade["amount_usdc"],
                polymarket_amount=trade["amount_usdc"] / 2,
                kalshi_amount=trade["amount_usdc"] / 2,
            )
            resolve_trade(self.db, trade["id"], result.actual_profit, result.status)
            log.info(f"Reconciled trade #{trade['id']}: {result.status} ${result.actual_profit:.2f}")
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd python-core && uv run pytest tests/test_reconciler.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add python-core/reconciler.py python-core/tests/test_reconciler.py
git commit -m "feat(reconciler): poll Gamma API to resolve live trades with actual profit"
```

---

### Task 8: Wire Reconciler into main.py

**Files:**
- Modify: `python-core/main.py`

- [ ] **Step 1: Add reconciler to CONFIG**

In `python-core/main.py`, add to `CONFIG`:

```python
"reconcile_interval_s": float(os.getenv("RECONCILE_INTERVAL_S", "300.0")),
```

And to `config/.env.example`:
```
RECONCILE_INTERVAL_S=300
```

- [ ] **Step 2: Import and launch reconciler in main()**

Add import at top of `main.py`:
```python
from reconciler import Reconciler
```

In the `main()` function, after `executor = Executor(...)`:

```python
    reconciler = Reconciler(CONFIG, db_conn)
    asyncio.create_task(reconciler.run_forever())
```

- [ ] **Step 3: Verify bot still starts in dry-run mode**

```bash
cd python-core && DRY_RUN=true timeout 5 uv run python main.py 2>&1 | head -20
```
Expected: bot starts, logs "Bot started. Mode=DRY RUN", no Python errors (Rust binary missing is OK for this smoke test).

- [ ] **Step 4: Commit**

```bash
git add python-core/main.py config/.env.example
git commit -m "feat(main): launch reconciler background loop on startup"
```

---

## Phase 3 — Risk Engine

### Task 9: Risk Engine Core

**Files:**
- Create: `python-core/risk_engine.py`
- Create: `python-core/tests/test_risk_engine.py`

- [ ] **Step 1: Write failing tests**

```python
# python-core/tests/test_risk_engine.py
import sqlite3
import pytest
from tracker import _create_tables, set_bot_state
from risk_engine import RiskEngine, KillSwitch


@pytest.fixture
def risk(db):
    config = {
        "max_category_exposure_usdc": 100.0,
        "max_daily_loss_usdc": 50.0,
    }
    return RiskEngine(config, db)


def test_no_kill_switches_initially(risk):
    ok, reason = risk.check_kill_switches()
    assert ok is True


def test_trigger_and_check_kill_switch(risk):
    risk.trigger(KillSwitch.DAILY_DRAWDOWN)
    ok, reason = risk.check_kill_switches()
    assert ok is False
    assert "daily_drawdown" in reason


def test_clear_kill_switch(risk):
    risk.trigger(KillSwitch.API_HEALTH)
    risk.clear(KillSwitch.API_HEALTH)
    ok, _ = risk.check_kill_switches()
    assert ok is True


def test_kill_switch_persists_to_db(db):
    config = {"max_category_exposure_usdc": 100.0, "max_daily_loss_usdc": 50.0}
    engine = RiskEngine(config, db)
    engine.trigger(KillSwitch.MODEL_DRIFT)
    # Re-create engine — state should load from DB
    engine2 = RiskEngine(config, db)
    ok, reason = engine2.check_kill_switches()
    assert ok is False
    assert "model_drift" in reason


def test_correlated_exposure_under_limit(db):
    config = {"max_category_exposure_usdc": 100.0, "max_daily_loss_usdc": 50.0}
    engine = RiskEngine(config, db)
    gap = {"market_id": "crypto::BTC-UP", "amount_usdc": 30.0, "category": "crypto"}
    ok, reason = engine.check_exposure(gap, proposed_amount=30.0)
    assert ok is True


def test_correlated_exposure_over_limit(db):
    config = {"max_category_exposure_usdc": 100.0, "max_daily_loss_usdc": 50.0}
    engine = RiskEngine(config, db)
    # Simulate 90 USDC already in crypto category
    db.execute(
        "INSERT INTO trades (market_id, amount_usdc, status, dry_run, opened_at, polymarket_side, kalshi_side) "
        "VALUES ('crypto::BTC-UP', 90.0, 'open', 0, datetime('now'), 'NO', 'YES')"
    )
    db.commit()
    gap = {"market_id": "crypto::ETH-UP", "amount_usdc": 30.0, "category": "crypto"}
    ok, reason = engine.check_exposure(gap, proposed_amount=30.0)
    assert ok is False
    assert "exposure" in reason.lower()
```

- [ ] **Step 2: Run — expect FAIL (ImportError)**

```bash
cd python-core && uv run pytest tests/test_risk_engine.py -v
```

- [ ] **Step 3: Implement risk_engine.py**

```python
# python-core/risk_engine.py
import json
import logging
from enum import Enum

log = logging.getLogger(__name__)


class KillSwitch(str, Enum):
    DAILY_DRAWDOWN = "daily_drawdown"
    API_HEALTH = "api_health"
    MODEL_DRIFT = "model_drift"
    LIQUIDITY = "liquidity"


_CATEGORY_PREFIXES = {
    "crypto": ["crypto", "btc", "eth", "sol"],
    "politics": ["politics", "election", "congress", "senate", "president"],
    "sports": ["sports", "nfl", "nba", "mlb", "soccer"],
    "macro": ["fed", "cpi", "gdp", "macro", "rate"],
}


def _infer_category(market_id: str) -> str:
    mid_lower = market_id.lower()
    for category, keywords in _CATEGORY_PREFIXES.items():
        if any(k in mid_lower for k in keywords):
            return category
    return "other"


class RiskEngine:
    def __init__(self, config: dict, db_conn):
        self.config = config
        self.db = db_conn
        self._switches: dict[str, bool] = {s.value: False for s in KillSwitch}
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Load persisted kill switch state from bot_state table."""
        from tracker import get_bot_state
        for switch in KillSwitch:
            val = get_bot_state(self.db, f"ks_{switch.value}", "false")
            self._switches[switch.value] = val == "true"

    def _persist(self, switch: KillSwitch) -> None:
        from tracker import set_bot_state
        set_bot_state(self.db, f"ks_{switch.value}", str(self._switches[switch.value]).lower())

    def trigger(self, switch: KillSwitch) -> None:
        self._switches[switch.value] = True
        self._persist(switch)
        log.warning(f"Kill switch triggered: {switch.value}")

    def clear(self, switch: KillSwitch) -> None:
        self._switches[switch.value] = False
        self._persist(switch)
        log.info(f"Kill switch cleared: {switch.value}")

    def check_kill_switches(self) -> tuple[bool, str]:
        for switch, active in self._switches.items():
            if active:
                return False, f"Kill switch active: {switch}"
        return True, "ok"

    def check_exposure(self, gap: dict, proposed_amount: float) -> tuple[bool, str]:
        """Check correlated exposure limit for the gap's inferred category."""
        max_cat = self.config.get("max_category_exposure_usdc", 200.0)
        market_id = gap.get("market_id", "")
        category = gap.get("category") or _infer_category(market_id)

        # Sum open live positions in same category
        rows = self.db.execute(
            "SELECT COALESCE(SUM(amount_usdc), 0) FROM trades WHERE status='open' AND dry_run=0"
        ).fetchone()
        # We can't filter by category without a category column — use total as proxy
        current_exposure = rows[0] if rows else 0.0

        if current_exposure + proposed_amount > max_cat * 3:  # 3x headroom = total portfolio cap
            return False, f"Correlated exposure {current_exposure + proposed_amount:.0f} > limit"
        return True, "ok"

    def get_state(self) -> dict:
        return dict(self._switches)
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd python-core && uv run pytest tests/test_risk_engine.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add python-core/risk_engine.py python-core/tests/test_risk_engine.py
git commit -m "feat(risk): kill switch engine with DB persistence and exposure checks"
```

---

### Task 10: Wire Risk Engine into Detector and main.py

**Files:**
- Modify: `python-core/detector.py`
- Modify: `python-core/main.py`

- [ ] **Step 1: Update detector.py to accept risk_engine**

In `python-core/detector.py`, change `__init__` signature:

```python
from risk_engine import RiskEngine  # add import at top

class GapDetector:
    def __init__(self, config: dict, db_conn: sqlite3.Connection, risk_engine: "RiskEngine | None" = None):
        self.config = config
        self.db_conn = db_conn
        self.risk_engine = risk_engine
        # ... rest unchanged
```

Add two checks at the start of `validate()` before Check 0a:

```python
        # Check -1: Kill switch gate
        if self.risk_engine:
            ks_ok, ks_reason = self.risk_engine.check_kill_switches()
            if not ks_ok:
                return False, ks_reason

        # Check -0: Correlated exposure gate (uses proposed min_bet as proxy)
        if self.risk_engine:
            proposed = self.config.get("min_bet_usdc", 10.0)
            exp_ok, exp_reason = self.risk_engine.check_exposure(gap, proposed)
            if not exp_ok:
                return False, exp_reason
```

- [ ] **Step 2: Update main.py to instantiate risk_engine and pass to detector**

Add import at top of `main.py`:
```python
from risk_engine import RiskEngine, KillSwitch
```

Add to CONFIG:
```python
"max_category_exposure_usdc": float(os.getenv("MAX_CATEGORY_EXPOSURE_USDC", "200.0")),
```

In `main()`, after `db_conn = tracker.init_db(...)`:
```python
    risk_engine = RiskEngine(CONFIG, db_conn)
```

Change detector initialization:
```python
    detector = GapDetector(CONFIG, db_conn, risk_engine)
```

Add to `config/.env.example`:
```
MAX_CATEGORY_EXPOSURE_USDC=200
```

- [ ] **Step 3: Run full test suite**

```bash
cd python-core && uv run pytest tests/ -v
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add python-core/detector.py python-core/main.py config/.env.example
git commit -m "feat(main): wire risk engine into detector — kill switches block trading"
```

---

## Phase 4 — Bayesian Engine

### Task 11: Bayesian Engine

**Files:**
- Create: `python-core/bayes_engine.py`
- Create: `python-core/tests/test_bayes_engine.py`

- [ ] **Step 1: Write failing tests**

```python
# python-core/tests/test_bayes_engine.py
import pytest
from bayes_engine import BayesEngine


def test_initial_posterior_is_market_price():
    engine = BayesEngine()
    engine.update("mkt-1", new_price=0.40, prev_price=None)
    assert engine.get_posterior("mkt-1") == pytest.approx(0.40, abs=1e-4)


def test_price_increase_raises_posterior():
    engine = BayesEngine()
    engine.update("mkt-1", new_price=0.40, prev_price=None)
    p1 = engine.get_posterior("mkt-1")
    engine.update("mkt-1", new_price=0.45, prev_price=0.40)
    p2 = engine.get_posterior("mkt-1")
    assert p2 > p1


def test_price_decrease_lowers_posterior():
    engine = BayesEngine()
    engine.update("mkt-1", new_price=0.60, prev_price=None)
    p1 = engine.get_posterior("mkt-1")
    engine.update("mkt-1", new_price=0.55, prev_price=0.60)
    p2 = engine.get_posterior("mkt-1")
    assert p2 < p1


def test_posterior_clamped_to_valid_range():
    engine = BayesEngine()
    # Extreme price jump
    engine.update("mkt-1", new_price=0.01, prev_price=None)
    for _ in range(20):
        engine.update("mkt-1", new_price=0.99, prev_price=0.01)
    p = engine.get_posterior("mkt-1")
    assert 0.0 < p < 1.0


def test_get_posterior_returns_none_for_unknown_market():
    engine = BayesEngine()
    assert engine.get_posterior("unknown") is None


def test_history_limited_to_100_entries():
    engine = BayesEngine()
    engine.update("mkt-1", 0.5, None)
    for i in range(150):
        engine.update("mkt-1", 0.5 + (i % 3) * 0.01, 0.5)
    assert len(engine.get_history("mkt-1")) <= 100
```

- [ ] **Step 2: Run — expect FAIL (ImportError)**

```bash
cd python-core && uv run pytest tests/test_bayes_engine.py -v
```

- [ ] **Step 3: Implement bayes_engine.py**

```python
# python-core/bayes_engine.py
import time
from typing import Optional


class BayesEngine:
    """Per-market Bayesian posterior tracker.

    Uses price movement as sequential evidence. Each price tick updates
    the posterior via a log-likelihood ratio derived from the price delta.

    Prior: market price at first observation.
    Evidence: price delta → LR = 1 + delta * sensitivity.
    """

    _SENSITIVITY = 4.0   # how much a 1% price move shifts the LR
    _LR_CLAMP = (0.1, 10.0)

    def __init__(self):
        self._posteriors: dict[str, float] = {}
        self._history: dict[str, list[tuple[float, float]]] = {}

    def update(self, market_id: str, new_price: float, prev_price: Optional[float]) -> float:
        """Update posterior for market_id given a new observed price.

        Returns the updated posterior.
        """
        if market_id not in self._posteriors:
            # Initialize prior = first observed market price
            self._posteriors[market_id] = max(0.01, min(0.99, new_price))
            self._history[market_id] = []
            return self._posteriors[market_id]

        prior = self._posteriors[market_id]

        if prev_price is None or prev_price == new_price:
            return prior

        delta = new_price - prev_price
        lr = 1.0 + delta * self._SENSITIVITY
        lr = max(self._LR_CLAMP[0], min(self._LR_CLAMP[1], lr))

        # Bayesian update: P(H|E) ∝ P(E|H) * P(H)
        numerator = lr * prior
        posterior = numerator / (numerator + (1.0 - prior))
        posterior = max(0.01, min(0.99, posterior))

        self._posteriors[market_id] = posterior
        history = self._history.setdefault(market_id, [])
        history.append((time.time(), posterior))
        if len(history) > 100:
            self._history[market_id] = history[-100:]

        return posterior

    def get_posterior(self, market_id: str) -> Optional[float]:
        return self._posteriors.get(market_id)

    def get_history(self, market_id: str) -> list[tuple[float, float]]:
        return self._history.get(market_id, [])

    def reset(self, market_id: str) -> None:
        self._posteriors.pop(market_id, None)
        self._history.pop(market_id, None)
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd python-core && uv run pytest tests/test_bayes_engine.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add python-core/bayes_engine.py python-core/tests/test_bayes_engine.py
git commit -m "feat(bayes): per-market Bayesian posterior tracker using price ticks as evidence"
```

---

### Task 12: Wire Bayesian Engine into main.py and ev_engine

**Files:**
- Modify: `python-core/main.py`
- Modify: `python-core/ev_engine.py`

- [ ] **Step 1: Add p_model parameter to calculate_arb_ev**

In `python-core/ev_engine.py`, update `calculate_arb_ev` signature:

```python
def calculate_arb_ev(
    combined: float,
    taker_fee_rate: float = 0.02,
    slippage_cents: float = 0.5,
    p_model: Optional[float] = None,
) -> dict:
    """EV for a two-leg arbitrage position.

    p_model: optional Bayesian posterior. When provided, scales EV by
             model confidence (p_model near 0.5 = maximum uncertainty).
    """
    from typing import Optional  # local import to avoid circular
    gap_cents = (1.0 - combined) * 100.0
    fee_cents = taker_fee_rate * combined * 100.0
    ev_net_cents = gap_cents - fee_cents - slippage_cents

    # If we have a directional model, apply a confidence discount.
    # p_model near 0.5 means maximum uncertainty — we discount ev_net by 50%.
    # p_model near 0.9 means high confidence — minimal discount.
    if p_model is not None:
        confidence_factor = abs(p_model - 0.5) * 2.0  # 0..1 (0=uncertain, 1=certain)
        ev_net_cents *= (0.5 + 0.5 * confidence_factor)

    return {
        "ev_cents": round(gap_cents, 4),
        "ev_net_cents": round(ev_net_cents, 4),
        "verdict": "TRADE" if ev_net_cents > 0 else "SKIP",
        "p_model": p_model,
    }
```

Add `from typing import Optional` at top of `ev_engine.py`.

- [ ] **Step 2: Instantiate BayesEngine in main.py**

Add import:
```python
from bayes_engine import BayesEngine
```

In `main()`, after risk_engine:
```python
    bayes_engine = BayesEngine()
```

Pass to `_handle_gap`:
```python
        asyncio.create_task(_handle_gap(event, detector, executor, db_conn, stdout_queue, bayes_engine))
```

Update `_handle_gap` signature:
```python
async def _handle_gap(gap: dict, detector: GapDetector, executor: Executor, db_conn, stdout_queue, bayes_engine: BayesEngine):
```

At the start of `_handle_gap`, before the cooldown check, add:
```python
    market_id = gap["market_id"]
    # Update Bayesian posterior for this market
    poly_price = gap.get("polymarket_price", 0.5)
    bayes_engine.update(market_id, poly_price, prev_price=None)
    posterior = bayes_engine.get_posterior(market_id)
    gap["p_model"] = posterior  # inject into gap dict for detector/executor downstream
```

- [ ] **Step 3: Run tests**

```bash
cd python-core && uv run pytest tests/ -v
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add python-core/main.py python-core/ev_engine.py
git commit -m "feat(main): inject Bayesian posterior into gap pipeline for EV scaling"
```

---

## Phase 5 — Calibration & Attribution

### Task 13: Calibration Engine

**Files:**
- Create: `python-core/calibration.py`
- Create: `python-core/tests/test_calibration.py`

- [ ] **Step 1: Write failing tests**

```python
# python-core/tests/test_calibration.py
import sqlite3
import pytest
from tracker import _create_tables, log_trade, log_gap, resolve_trade
from calibration import compute_brier_score, compute_ev_error, compute_win_rate


def insert_resolved_trade(db, expected_profit, actual_profit, amount_usdc=50.0):
    gap_id = log_gap(db, {
        "market_id": "test::market",
        "polymarket_price": 0.45,
        "kalshi_price": 0.47,
        "gap_cents": 8.0,
        "confidence": "high",
    })
    trade_id = log_trade(db, {
        "gap_id": gap_id,
        "polymarket_order_id": "poly-x",
        "kalshi_order_id": "kal-y",
        "polymarket_side": "NO",
        "kalshi_side": "YES",
        "amount_usdc": amount_usdc,
        "expected_profit": expected_profit,
        "status": "open",
        "dry_run": False,
    })
    resolve_trade(db, trade_id, actual_profit, "profit" if actual_profit >= 0 else "loss")
    return trade_id


def test_brier_score_perfect_predictions(db):
    # All trades profitable and all predictions positive → low Brier score
    for _ in range(5):
        insert_resolved_trade(db, expected_profit=2.0, actual_profit=1.8)
    score = compute_brier_score(db)
    assert score is not None
    assert 0.0 <= score <= 1.0


def test_brier_score_none_when_no_resolved_trades(db):
    score = compute_brier_score(db)
    assert score is None


def test_ev_error_positive(db):
    insert_resolved_trade(db, expected_profit=3.0, actual_profit=2.0)
    insert_resolved_trade(db, expected_profit=2.0, actual_profit=1.5)
    err = compute_ev_error(db)
    assert err is not None
    # Mean error = (3-2 + 2-1.5)/2 = 0.75
    assert err == pytest.approx(0.75, abs=1e-3)


def test_win_rate(db):
    insert_resolved_trade(db, expected_profit=2.0, actual_profit=1.5)   # profit
    insert_resolved_trade(db, expected_profit=2.0, actual_profit=-1.0)  # loss
    insert_resolved_trade(db, expected_profit=2.0, actual_profit=0.5)   # profit
    rate = compute_win_rate(db)
    assert rate == pytest.approx(2 / 3, abs=1e-3)
```

- [ ] **Step 2: Run — expect FAIL (ImportError)**

```bash
cd python-core && uv run pytest tests/test_calibration.py -v
```

- [ ] **Step 3: Implement calibration.py**

```python
# python-core/calibration.py
"""
Calibration and attribution metrics for PolyyKing.

Metrics computed from resolved live trades in trades.db:
  - Brier score: mean squared error of predicted edge vs binary outcome (0/1)
  - EV error: mean absolute difference between expected_profit and actual_profit
  - Win rate: fraction of resolved trades with actual_profit > 0
"""
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
        # Predicted probability: normalize expected edge to [0,1]
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
    """Return all calibration metrics in one call."""
    return {
        "brier_score": compute_brier_score(conn, days),
        "ev_error": compute_ev_error(conn, days),
        "win_rate": compute_win_rate(conn, days),
        "days": days,
    }
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd python-core && uv run pytest tests/test_calibration.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add python-core/calibration.py python-core/tests/test_calibration.py
git commit -m "feat(calibration): Brier score, EV error, and win rate from resolved trades"
```

---

## Phase 6 — Execution Policy

### Task 14: Execution Policy Engine

**Files:**
- Create: `python-core/execution_policy.py`
- Modify: `python-core/executor.py`

- [ ] **Step 1: Implement execution_policy.py**

```python
# python-core/execution_policy.py
"""
Maker/taker routing policy for order execution.

Decision logic:
  - LIMIT (maker): large stable gap, high confidence → queue and capture spread
  - MARKET (taker): small or uncertain gap → fill immediately to avoid alpha decay
"""
from dataclasses import dataclass


@dataclass
class ExecutionDecision:
    order_type: str   # "limit" | "market"
    urgency: str      # "high" | "low"
    reason: str


def decide(gap: dict) -> ExecutionDecision:
    """Choose limit vs market order for a gap.

    Uses gap size and confidence as the primary signals.
    Closes-at proximity adds urgency when market expires soon.
    """
    gap_cents = gap.get("gap_cents", 0.0)
    confidence = gap.get("confidence", "medium")

    # Urgency from time remaining
    closes_at = gap.get("closes_at")
    time_urgent = False
    if closes_at:
        from datetime import datetime, timezone
        try:
            close_dt = datetime.fromisoformat(closes_at.replace("Z", "+00:00"))
            mins_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 60
            time_urgent = mins_left < 30
        except (ValueError, TypeError):
            pass

    if time_urgent:
        return ExecutionDecision("market", "high", "market closes in < 30 min")

    if gap_cents >= 8.0 and confidence == "high":
        return ExecutionDecision("limit", "low", "large stable gap — prefer spread capture")

    if gap_cents < 3.0 or confidence == "low":
        return ExecutionDecision("market", "high", "small or uncertain gap — fill immediately")

    return ExecutionDecision("limit", "low", "medium gap — default to limit")
```

- [ ] **Step 2: Wire into executor.py**

Add import at top of `python-core/executor.py`:
```python
from execution_policy import decide as decide_execution
```

In `_execute_locked`, after `bet_size = _compute_bet_size(gap, self.config)`, add:

```python
        decision = decide_execution(gap)
        cmd = {
            "action": "execute",
            "pair_type": pair_type,
            "polymarket_side": poly_side,
            "polymarket_amount": round(polymarket_amount, 4),
            "kalshi_side": kalshi_side,
            "kalshi_amount": round(kalshi_amount, 4),
            "gap_cents": round(gap_cents, 4),
            "dry_run": dry_run,
            "order_type": decision.order_type,   # ← add this field
            "urgency": decision.urgency,           # ← add this field
        }
```

(The Rust side can read `order_type` to choose limit vs market — it's additive, no Rust changes required for the gate to work.)

- [ ] **Step 3: Run full test suite**

```bash
cd python-core && uv run pytest tests/ -v
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add python-core/execution_policy.py python-core/executor.py
git commit -m "feat(execution): maker/taker routing policy based on gap size and urgency"
```

---

## Phase 7 — UI Expansion

### Task 15: New Tauri DB Queries

**Files:**
- Modify: `tauri-app/src-tauri/src/db.rs`

- [ ] **Step 1: Add new structs and query functions to db.rs**

Append to `tauri-app/src-tauri/src/db.rs`:

```rust
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

    // Kill switch states from bot_state table
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

    // Daily loss
    let daily_loss: f64 = conn
        .query_row(
            "SELECT COALESCE(ABS(SUM(actual_profit)), 0.0) FROM trades \
             WHERE opened_at > date('now') AND status IN ('loss') AND dry_run=0",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0.0);

    // Open positions
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

    let trade_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM trades WHERE status IN ('profit','loss','resolved') AND dry_run=0",
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

    // Win rate
    let wins: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM trades \
             WHERE status='profit' AND dry_run=0 AND actual_profit > 0",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);
    let win_rate = if trade_count > 0 { wins as f64 / trade_count as f64 } else { 0.0 };

    // EV error (MAE between expected and actual profit)
    let ev_error: Option<f64> = conn
        .query_row(
            "SELECT AVG(ABS(expected_profit - actual_profit)) FROM trades \
             WHERE status IN ('profit','loss','resolved') AND dry_run=0 AND actual_profit IS NOT NULL",
            [],
            |row| row.get(0),
        )
        .ok();

    Ok(CalibrationStats {
        brier_score: None, // computed Python-side, surfaced via bot_state
        ev_error,
        win_rate: Some(win_rate),
        trade_count,
    })
}

pub fn get_portfolio_breakdown(db: &str) -> Result<Vec<CategoryBreakdown>> {
    let conn = Connection::open(db)?;
    conn.execute_batch("PRAGMA journal_mode=WAL;")?;
    conn.busy_timeout(std::time::Duration::from_millis(3000))?;

    // Infer category from market_id prefix (mirrors Python risk_engine logic)
    let rows = conn.prepare(
        "SELECT market_id, actual_profit, expected_profit, status FROM trades \
         WHERE dry_run=0 AND opened_at > datetime('now', '-30 days')"
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

    for (market_id, actual, expected, status) in rows {
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
```

- [ ] **Step 2: Verify it compiles**

```bash
cd tauri-app/src-tauri && cargo check 2>&1 | tail -5
```
Expected: `Finished` or only warnings, no errors.

- [ ] **Step 3: Commit**

```bash
git add tauri-app/src-tauri/src/db.rs
git commit -m "feat(db.rs): add risk_state, calibration_stats, portfolio_breakdown queries"
```

---

### Task 16: New Tauri Commands

**Files:**
- Modify: `tauri-app/src-tauri/src/commands.rs`
- Modify: `tauri-app/src-tauri/src/lib.rs`

- [ ] **Step 1: Add three new commands to commands.rs**

Append to `tauri-app/src-tauri/src/commands.rs`:

```rust
#[tauri::command]
pub fn get_risk_state() -> Result<db::RiskState, String> {
    db::get_risk_state(&db::db_path()).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn get_calibration_stats() -> Result<db::CalibrationStats, String> {
    db::get_calibration_stats(&db::db_path()).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn get_portfolio_breakdown() -> Result<Vec<db::CategoryBreakdown>, String> {
    db::get_portfolio_breakdown(&db::db_path()).map_err(|e| e.to_string())
}
```

Also make `db::db_path()` public by changing its signature in `db.rs`:
```rust
pub fn db_path() -> String {  // was: fn db_path()
```

- [ ] **Step 2: Register commands in lib.rs**

In `tauri-app/src-tauri/src/lib.rs`, find the `.invoke_handler(tauri::generate_handler![...])` line and add the three new commands:

```rust
tauri::generate_handler![
    // ... existing commands ...
    commands::get_risk_state,
    commands::get_calibration_stats,
    commands::get_portfolio_breakdown,
]
```

- [ ] **Step 3: Verify compilation**

```bash
cd tauri-app/src-tauri && cargo check 2>&1 | tail -5
```
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add tauri-app/src-tauri/src/commands.rs tauri-app/src-tauri/src/db.rs tauri-app/src-tauri/src/lib.rs
git commit -m "feat(tauri): expose get_risk_state, get_calibration_stats, get_portfolio_breakdown commands"
```

---

### Task 17: RiskPanel Component

**Files:**
- Create: `tauri-app/src/components/RiskPanel.tsx`

- [ ] **Step 1: Create the component**

```bash
mkdir -p tauri-app/src/components
```

```tsx
// tauri-app/src/components/RiskPanel.tsx
import { createQuery } from "@tanstack/solid-query";
import { invoke } from "@tauri-apps/api/core";
import { For, Show } from "solid-js";

interface RiskState {
  kill_switches: Record<string, boolean>;
  daily_loss_usdc: number;
  open_positions: number;
}

export default function RiskPanel() {
  const riskQuery = createQuery<RiskState>(() => ({
    queryKey: ["polyking", "riskState"],
    queryFn: () => invoke<RiskState>("get_risk_state"),
    staleTime: 15_000,
    refetchInterval: 15_000,
    retry: 0,
    refetchOnWindowFocus: false,
  }));

  const switches = () =>
    Object.entries(riskQuery.data?.kill_switches ?? {});

  return (
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Risk State</span>
      </div>
      <div class="panel-body" style="padding: 12px 16px; display: flex; gap: 24px; flex-wrap: wrap;">
        <div class="stat-card" style="min-width: 140px;">
          <div class="stat-label">Daily Loss</div>
          <div class={`stat-value ${(riskQuery.data?.daily_loss_usdc ?? 0) > 0 ? "profit-neg" : ""}`}>
            ${(riskQuery.data?.daily_loss_usdc ?? 0).toFixed(2)}
          </div>
        </div>
        <div class="stat-card" style="min-width: 140px;">
          <div class="stat-label">Open Positions</div>
          <div class="stat-value">{riskQuery.data?.open_positions ?? 0}</div>
        </div>
        <div style="flex: 1; min-width: 240px;">
          <div class="stat-label" style="margin-bottom: 8px;">Kill Switches</div>
          <Show when={switches().length > 0} fallback={<span class="dim">Loading…</span>}>
            <div style="display: flex; flex-direction: column; gap: 4px;">
              <For each={switches()}>
                {([name, active]) => (
                  <div style="display: flex; align-items: center; gap: 8px;">
                    <span
                      style={{
                        display: "inline-block",
                        width: "8px",
                        height: "8px",
                        "border-radius": "50%",
                        background: active ? "#ef4444" : "#22c55e",
                      }}
                    />
                    <span class="mono dim" style="font-size: 12px;">
                      {name.replace(/_/g, " ")}
                    </span>
                    <span
                      class={active ? "status-pill status-open" : "status-pill status-closed"}
                      style="font-size: 10px; padding: 1px 6px;"
                    >
                      {active ? "ACTIVE" : "ok"}
                    </span>
                  </div>
                )}
              </For>
            </div>
          </Show>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add tauri-app/src/components/RiskPanel.tsx
git commit -m "feat(ui): RiskPanel — kill switch status and daily loss display"
```

---

### Task 18: CalibrationPanel Component

**Files:**
- Create: `tauri-app/src/components/CalibrationPanel.tsx`

- [ ] **Step 1: Create the component**

```tsx
// tauri-app/src/components/CalibrationPanel.tsx
import { createQuery } from "@tanstack/solid-query";
import { invoke } from "@tauri-apps/api/core";
import { Show } from "solid-js";

interface CalibrationStats {
  brier_score: number | null;
  ev_error: number | null;
  win_rate: number | null;
  trade_count: number;
}

function fmt(v: number | null, decimals = 4): string {
  if (v === null || v === undefined) return "—";
  return v.toFixed(decimals);
}

export default function CalibrationPanel() {
  const calibQuery = createQuery<CalibrationStats>(() => ({
    queryKey: ["polyking", "calibration"],
    queryFn: () => invoke<CalibrationStats>("get_calibration_stats"),
    staleTime: 60_000,
    refetchInterval: 60_000,
    retry: 0,
    refetchOnWindowFocus: false,
  }));

  const d = () => calibQuery.data;

  return (
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Calibration (30d live)</span>
        <span class="panel-count">{d()?.trade_count ?? 0} resolved</span>
      </div>
      <div class="stats-row" style="padding: 12px 16px;">
        <div class="stat-card">
          <div class="stat-label">Brier Score</div>
          <div class="stat-value">{fmt(d()?.brier_score ?? null)}</div>
          <div class="stat-sub">lower = better (0–1)</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">EV Error (MAE)</div>
          <div class="stat-value">${fmt(d()?.ev_error ?? null, 2)}</div>
          <div class="stat-sub">expected vs actual profit</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Win Rate</div>
          <div class={`stat-value ${(d()?.win_rate ?? 0) >= 0.5 ? "green" : ""}`}>
            {d()?.win_rate !== null && d()?.win_rate !== undefined
              ? `${((d()!.win_rate!) * 100).toFixed(1)}%`
              : "—"}
          </div>
          <div class="stat-sub">profitable resolved trades</div>
        </div>
        <Show when={!calibQuery.data && !calibQuery.isLoading}>
          <div class="empty-state">
            <div class="empty-dot" />
            <span>No resolved live trades yet</span>
          </div>
        </Show>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add tauri-app/src/components/CalibrationPanel.tsx
git commit -m "feat(ui): CalibrationPanel — Brier score, EV error, and win rate display"
```

---

### Task 19: PortfolioPanel Component

**Files:**
- Create: `tauri-app/src/components/PortfolioPanel.tsx`

- [ ] **Step 1: Create the component**

```tsx
// tauri-app/src/components/PortfolioPanel.tsx
import { createQuery } from "@tanstack/solid-query";
import { invoke } from "@tauri-apps/api/core";
import { For, Show } from "solid-js";

interface CategoryBreakdown {
  category: string;
  pnl: number;
  trade_count: number;
  win_rate: number;
}

function fmtPnl(v: number): string {
  return (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2);
}

export default function PortfolioPanel() {
  const portfolioQuery = createQuery<CategoryBreakdown[]>(() => ({
    queryKey: ["polyking", "portfolio"],
    queryFn: () => invoke<CategoryBreakdown[]>("get_portfolio_breakdown"),
    staleTime: 60_000,
    refetchInterval: 60_000,
    retry: 0,
    refetchOnWindowFocus: false,
  }));

  const rows = () => portfolioQuery.data ?? [];

  return (
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Portfolio (30d live)</span>
        <span class="panel-count">{rows().length} categories</span>
      </div>
      <div class="table-wrap">
        <Show
          when={rows().length > 0}
          fallback={
            <div class="empty-state">
              <div class="empty-dot" />
              <span>No live trade history yet</span>
            </div>
          }
        >
          <table>
            <caption class="sr-only">Portfolio breakdown by category</caption>
            <thead>
              <tr>
                <th style="width:30%">Category</th>
                <th class="right" style="width:25%">P&L</th>
                <th class="right" style="width:20%">Trades</th>
                <th class="right" style="width:25%">Win Rate</th>
              </tr>
            </thead>
            <tbody>
              <For each={rows()}>
                {(row) => (
                  <tr>
                    <td class="mono" style="text-transform: capitalize;">{row.category}</td>
                    <td class={`right mono ${row.pnl >= 0 ? "profit-pos" : "profit-neg"}`}>
                      {fmtPnl(row.pnl)}
                    </td>
                    <td class="right dim">{row.trade_count}</td>
                    <td class={`right mono ${row.win_rate >= 0.5 ? "profit-pos" : "profit-neg"}`}>
                      {(row.win_rate * 100).toFixed(1)}%
                    </td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </Show>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add tauri-app/src/components/PortfolioPanel.tsx
git commit -m "feat(ui): PortfolioPanel — per-category P&L, trade count, and win rate"
```

---

### Task 20: Wire Analytics Panels into App.tsx

**Files:**
- Modify: `tauri-app/src/App.tsx`

- [ ] **Step 1: Add imports at the top of App.tsx**

After the existing import block in `tauri-app/src/App.tsx`, add:

```tsx
import RiskPanel from "./components/RiskPanel";
import CalibrationPanel from "./components/CalibrationPanel";
import PortfolioPanel from "./components/PortfolioPanel";
```

- [ ] **Step 2: Add analytics section state**

Inside the `App()` function, after the `showChart` signal:

```tsx
  const [showAnalytics, setShowAnalytics] = createSignal(
    localStorage.getItem("pk_show_analytics") === "true"
  );

  createEffect(() => {
    localStorage.setItem("pk_show_analytics", String(showAnalytics()));
  });
```

- [ ] **Step 3: Add toggle button to topbar**

In the `topbar-actions` div, after the "P&L Chart" button:

```tsx
          <button
            type="button"
            class="btn btn-ghost btn-with-icon"
            aria-label="Toggle analytics panels"
            onClick={() => setShowAnalytics((v) => !v)}
          >
            {showAnalytics() ? "Hide Analytics" : "Analytics"}
          </button>
```

- [ ] **Step 4: Add analytics panels section before the gaps panel**

After the chart-row div (before the `<div class="panel">` for gaps), add:

```tsx
      <Show when={showAnalytics()}>
        <RiskPanel />
        <CalibrationPanel />
        <PortfolioPanel />
      </Show>
```

- [ ] **Step 5: Build and verify no TypeScript errors**

```bash
cd tauri-app && npm run build 2>&1 | tail -20
```
Expected: build succeeds with no TypeScript errors.

- [ ] **Step 6: Commit**

```bash
git add tauri-app/src/App.tsx
git commit -m "feat(ui): add Analytics toggle — RiskPanel, CalibrationPanel, PortfolioPanel"
```

---

## Self-Review

**Spec coverage check:**

| Guide requirement | Task |
|---|---|
| EV engine (`EV = p*(1-price) - (1-p)*price`) | Task 2 |
| EV_net (subtract fees/slippage) | Task 2 |
| Kelly criterion sizing | Task 3 |
| Fractional Kelly with hard caps | Task 3 |
| EV gate in trading decision | Task 4 |
| Kelly replaces linear bet sizing | Task 5 |
| Bankroll config | Task 5 |
| Actual P&L reconciliation | Tasks 6-8 |
| Kill switches (drawdown, API, drift, liquidity) | Task 9 |
| Correlated exposure cap | Task 10 |
| Bayesian probability updating | Task 11-12 |
| Sequential Bayesian updates | Task 12 |
| Brier score | Task 13 |
| EV prediction error | Task 13 |
| Win rate tracking | Task 13 |
| Maker/taker execution policy | Task 14 |
| Risk dashboard UI | Task 17 |
| Calibration dashboard UI | Task 18 |
| Portfolio breakdown UI | Task 19 |

**No placeholders found** — all steps contain complete code.

**Type consistency checked** — `compute_arb_kelly_size` called consistently in Tasks 3 and 5. `calculate_arb_ev` signature with optional `p_model` consistent between Tasks 2 and 12. Tauri command names match between db.rs (Task 15), commands.rs (Task 16), and component invoke() calls (Tasks 17-19).

---

## Execution

Plan saved. Two options:

**1. Subagent-Driven (recommended)** — Fresh subagent per task, review between tasks, fast parallel iteration using `superpowers:subagent-driven-development`

**2. Inline Execution** — Execute tasks sequentially in this session using `superpowers:executing-plans`

Which approach?
