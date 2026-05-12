# Live Trading Execution Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all order execution from Rust to Python so that Polymarket and Kalshi orders actually reach the exchanges, add cross-platform pair generation, and correct fee rates — bringing the bot from ~15% to ~80% live-trading readiness.

**Architecture:** Rust becomes a pure price feed (gap events only, one-directional stdout). Python handles all order placement: Polymarket via `py_clob_client` SDK (handles EIP-712 signing), Kalshi via direct REST with Python HMAC-SHA256. Both legs of every trade fire concurrently; a partial fill triggers an immediate emergency close on the filled leg.

**Tech Stack:** Python 3.11+, `py_clob_client>=0.19.0`, `aiohttp`, `hmac`/`hashlib` (stdlib), Rust (price feed only, no new crates), SQLite, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `python-core/kalshi_executor.py` | **Create** | Kalshi REST orders with HMAC-SHA256 signing |
| `python-core/polymarket_executor.py` | **Create** | Polymarket orders via py_clob_client SDK |
| `python-core/two_leg_executor.py` | **Create** | Concurrent leg placement, Kelly sizing, emergency close |
| `python-core/executor.py` | **Delete** | Replaced by the three above |
| `python-core/tracker.py` | **Modify** | Add gap_cents column, emergency_positions table, helper fns |
| `python-core/detector.py` | **Modify** | Use gap["fee_rate"] + per-pair-type min gap threshold |
| `python-core/reconciler.py` | **Modify** | Real profit from gap_cents, not hardcoded 8% |
| `python-core/main.py` | **Modify** | Startup backfill check, fee_rate_map, TwoLegExecutor |
| `python-core/pyproject.toml` | **Modify** | Add py-clob-client dependency |
| `scripts/backfill_matches.py` | **Modify** | Store fee_rate per pair, merge cross+internal |
| `rust-core/src/bridge.rs` | **Modify** | Remove stdin reader — gaps only, one-directional |
| `rust-core/src/main.rs` | **Modify** | Remove executor spawner and order channels |
| `rust-core/src/executor.rs` | **Modify** | Delete live_execute(), keep dry_run_execute() |
| `config/.env.example` | **Modify** | Add CROSS_PLATFORM_MIN_GAP_CENTS, INTERNAL_MIN_GAP_CENTS |

---

## Task 1: DB Migration — gap_cents column + emergency_positions table

**Files:**
- Modify: `python-core/tracker.py`
- Test: `python-core/tests/test_tracker.py`

The `trades` table needs a `gap_cents` column so the reconciler can compute actual profit. A new `emergency_positions` table records any partial fills requiring manual review.

- [ ] **Step 1: Write the failing tests**

Open `python-core/tests/test_tracker.py` and add at the end:

```python
def test_trades_has_gap_cents_column(db):
    db.execute(
        "INSERT INTO trades (market_id, amount_usdc, gap_cents, status, dry_run, opened_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("test-market", 10.0, 7.5, "open", 1, "2026-01-01T00:00:00"),
    )
    db.commit()
    row = db.execute("SELECT gap_cents FROM trades WHERE market_id='test-market'").fetchone()
    assert row is not None
    assert abs(row["gap_cents"] - 7.5) < 0.001


def test_emergency_positions_table_exists(db):
    db.execute(
        "INSERT INTO emergency_positions (market_id, platform, order_id, side, amount_usdc, opened_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("test-market", "polymarket", "ord_abc", "NO", 5.0, "2026-01-01T00:00:00", "open"),
    )
    db.commit()
    row = db.execute("SELECT * FROM emergency_positions WHERE order_id='ord_abc'").fetchone()
    assert row is not None
    assert row["status"] == "open"


def test_log_emergency_position(db):
    from tracker import log_emergency_position
    ep_id = log_emergency_position(db, {
        "market_id": "test-market",
        "platform": "kalshi",
        "order_id": "ord_xyz",
        "side": "YES",
        "amount_usdc": 10.0,
    })
    assert ep_id > 0
    row = db.execute("SELECT * FROM emergency_positions WHERE id=?", (ep_id,)).fetchone()
    assert row["platform"] == "kalshi"
    assert row["status"] == "open"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd python-core
uv run pytest tests/test_tracker.py::test_trades_has_gap_cents_column tests/test_tracker.py::test_emergency_positions_table_exists tests/test_tracker.py::test_log_emergency_position -v
```

Expected: FAIL — `gap_cents` column doesn't exist, `emergency_positions` table doesn't exist, `log_emergency_position` not defined.

- [ ] **Step 3: Add gap_cents to trades schema and create emergency_positions table**

Open `python-core/tracker.py`. In `_create_tables`, find the `trades` CREATE TABLE statement and add `gap_cents REAL` as a new column:

```python
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gap_id INTEGER REFERENCES gaps(id),
            polymarket_order_id TEXT,
            kalshi_order_id TEXT,
            polymarket_side TEXT,
            kalshi_side TEXT,
            amount_usdc REAL,
            gap_cents REAL,
            expected_profit REAL,
            actual_profit REAL,
            status TEXT,
            dry_run INTEGER,
            opened_at TEXT,
            resolved_at TEXT
        );
```

Then add the emergency_positions table inside `_create_tables` executescript (after the bot_state table):

```python
        CREATE TABLE IF NOT EXISTS emergency_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            platform TEXT,
            order_id TEXT,
            side TEXT,
            amount_usdc REAL,
            opened_at TEXT,
            closed_at TEXT,
            status TEXT
        );
```

Then find the migrations list (around line 88) and add the ALTER TABLE for existing DBs:

```python
        "ALTER TABLE trades ADD COLUMN gap_cents REAL",
```

Add it alongside the existing ALTER TABLE lines.

- [ ] **Step 4: Add log_emergency_position function**

In `python-core/tracker.py`, add this function after `log_trade`:

```python
def log_emergency_position(conn: sqlite3.Connection, ep: dict) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO emergency_positions
           (market_id, platform, order_id, side, amount_usdc, opened_at, status)
           VALUES (?, ?, ?, ?, ?, ?, 'open')""",
        (
            ep.get("market_id", ""),
            ep.get("platform", ""),
            ep.get("order_id", ""),
            ep.get("side", ""),
            ep.get("amount_usdc", 0.0),
            now,
        ),
    )
    conn.commit()
    return cursor.lastrowid
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd python-core
uv run pytest tests/test_tracker.py -v
```

Expected: all tests pass including the 3 new ones.

- [ ] **Step 6: Commit**

```bash
git add python-core/tracker.py python-core/tests/test_tracker.py
git commit -m "feat(db): add gap_cents to trades, emergency_positions table"
```

---

## Task 2: KalshiExecutor — Python REST with HMAC-SHA256

**Files:**
- Create: `python-core/kalshi_executor.py`
- Create: `python-core/tests/test_kalshi_executor.py`

The Kalshi REST API for live orders requires three custom headers: `Kalshi-Access-Key`, `Kalshi-Access-Signature` (HMAC-SHA256 of `timestamp + method + path`), and `Kalshi-Access-Timestamp`. Order count = number of contracts = `round(k)` where `k = bet_size / combined`.

- [ ] **Step 1: Write the failing tests**

Create `python-core/tests/test_kalshi_executor.py`:

```python
import base64
import hashlib
import hmac
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from kalshi_executor import KalshiExecutor, ExecutorError


@pytest.fixture
def config():
    return {
        "kalshi_api_key": "test_key",
        "kalshi_api_secret": "test_secret_32byteslong_padding!",
        "kalshi_api_url": "https://api.elections.kalshi.com/trade-api/v2",
        "dry_run": False,
    }


def test_sign_produces_correct_headers(config):
    ex = KalshiExecutor(config)
    headers = ex._sign("POST", "/trade-api/v2/portfolio/orders")

    assert "Kalshi-Access-Key" in headers
    assert headers["Kalshi-Access-Key"] == "test_key"
    assert "Kalshi-Access-Signature" in headers
    assert "Kalshi-Access-Timestamp" in headers
    # Signature must be valid base64
    base64.b64decode(headers["Kalshi-Access-Signature"])


def test_sign_hmac_is_correct(config):
    ex = KalshiExecutor(config)
    method = "POST"
    path = "/trade-api/v2/portfolio/orders"
    headers = ex._sign(method, path)
    timestamp = headers["Kalshi-Access-Timestamp"]

    # Recompute expected signature
    message = (timestamp + method + path).encode()
    expected_sig = base64.b64encode(
        hmac.new(config["kalshi_api_secret"].encode(), message, hashlib.sha256).digest()
    ).decode()

    assert headers["Kalshi-Access-Signature"] == expected_sig


@pytest.mark.asyncio
async def test_place_order_success(config):
    ex = KalshiExecutor(config)
    mock_response = MagicMock()
    mock_response.status = 201
    mock_response.json = AsyncMock(return_value={
        "order": {"order_id": "ord_abc123", "status": "resting"}
    })
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("kalshi_executor.aiohttp.ClientSession", return_value=mock_session):
        result = await ex.place_order("KXBTCD-25MAY31-B95000", "buy", 20)

    assert result["order_id"] == "ord_abc123"
    assert result["status"] == "resting"


@pytest.mark.asyncio
async def test_place_order_raises_on_non_201(config):
    ex = KalshiExecutor(config)
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.json = AsyncMock(return_value={"error": {"message": "auth failure"}})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("kalshi_executor.aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(ExecutorError, match="401"):
            await ex.place_order("KXBTCD-25MAY31-B95000", "buy", 20)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd python-core
uv run pytest tests/test_kalshi_executor.py -v
```

Expected: FAIL — `kalshi_executor` module doesn't exist.

- [ ] **Step 3: Implement KalshiExecutor**

Create `python-core/kalshi_executor.py`:

```python
import asyncio
import base64
import hashlib
import hmac as _hmac
import time
from typing import Optional

import aiohttp


class ExecutorError(Exception):
    pass


class KalshiExecutor:
    """Places live orders on Kalshi via their REST API with HMAC-SHA256 signing.

    count = number of contracts (integer).
    Caller computes: count = round(bet_size / combined) where combined = price_a + price_b.
    """

    def __init__(self, config: dict):
        self.api_key: str = config["kalshi_api_key"]
        self.api_secret: str = config["kalshi_api_secret"]
        self.api_url: str = config.get(
            "kalshi_api_url",
            "https://api.elections.kalshi.com/trade-api/v2",
        )

    def _sign(self, method: str, path: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        message = (timestamp + method + path).encode()
        signature = base64.b64encode(
            _hmac.new(self.api_secret.encode(), message, hashlib.sha256).digest()
        ).decode()
        return {
            "Authorization": f"Token {self.api_key}",
            "Kalshi-Access-Key": self.api_key,
            "Kalshi-Access-Signature": signature,
            "Kalshi-Access-Timestamp": timestamp,
            "Content-Type": "application/json",
        }

    async def place_order(self, ticker: str, action: str, count: int) -> dict:
        """Place a market order on Kalshi.

        ticker: Kalshi market ticker e.g. "KXBTCD-25MAY31-B95000"
        action: "buy" or "sell"
        count:  integer number of contracts
        """
        path = "/trade-api/v2/portfolio/orders"
        headers = self._sign("POST", path)
        body = {
            "ticker": ticker,
            "action": action,
            "count": int(count),
            "type": "market",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.api_url}/portfolio/orders",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if resp.status not in (200, 201):
                    raise ExecutorError(
                        f"Kalshi order failed HTTP {resp.status}: {data}"
                    )
                order = data.get("order", data)
                return {
                    "order_id": order.get("order_id", ""),
                    "status": order.get("status", ""),
                    "platform": "kalshi",
                    "ticker": ticker,
                    "count": count,
                }

    async def close_order(self, ticker: str, count: int) -> None:
        """Emergency close: sell back a filled position."""
        await self.place_order(ticker, "sell", count)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd python-core
uv run pytest tests/test_kalshi_executor.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add python-core/kalshi_executor.py python-core/tests/test_kalshi_executor.py
git commit -m "feat: KalshiExecutor with HMAC-SHA256 signing"
```

---

## Task 3: PolymarketExecutor — py_clob_client SDK

**Files:**
- Modify: `python-core/pyproject.toml`
- Create: `python-core/polymarket_executor.py`
- Create: `python-core/tests/test_polymarket_executor.py`

`py_clob_client` is the official Polymarket SDK. It handles EIP-712 signing, nonce management, and L1/L2 auth headers internally. We wrap it in an async interface since it is synchronous internally.

- [ ] **Step 1: Add py_clob_client to pyproject.toml**

Open `python-core/pyproject.toml` and add to dependencies:

```toml
[project]
name = "arb-python"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "aiohttp>=3.9",
    "python-dotenv>=1.0",
    "loguru>=0.7",
    "pydantic>=2.0",
    "rapidfuzz>=3.0",
    "py-clob-client>=0.19.0",
]
```

Install it:

```bash
cd python-core
uv sync
```

Expected: `py_clob_client` installed with no errors.

- [ ] **Step 2: Write the failing tests**

Create `python-core/tests/test_polymarket_executor.py`:

```python
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from polymarket_executor import PolymarketExecutor, ExecutorError


@pytest.fixture
def config():
    return {
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
        "dry_run": False,
    }


def make_mock_client(order_id="ord_poly_123", status="matched", error=""):
    client = MagicMock()
    client.create_or_derive_api_creds.return_value = MagicMock()
    client.create_market_order.return_value = MagicMock()
    client.post_order.return_value = {
        "orderID": order_id,
        "status": status,
        "errorMsg": error,
    }
    return client


@pytest.mark.asyncio
async def test_place_order_success(config):
    mock_client = make_mock_client()
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        result = await ex.place_order(
            token_id="1234567890abcdef",
            side="BUY",
            amount_usdc=5.0,
        )
    assert result["order_id"] == "ord_poly_123"
    assert result["status"] == "matched"
    assert result["platform"] == "polymarket"


@pytest.mark.asyncio
async def test_place_order_raises_on_error_msg(config):
    mock_client = make_mock_client(error="insufficient funds")
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        with pytest.raises(ExecutorError, match="insufficient funds"):
            await ex.place_order("token_id", "BUY", 5.0)


@pytest.mark.asyncio
async def test_close_order_calls_place_order(config):
    mock_client = make_mock_client()
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        # Should not raise
        await ex.close_order("token_id", 5.0)
    assert mock_client.post_order.call_count == 1
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd python-core
uv run pytest tests/test_polymarket_executor.py -v
```

Expected: FAIL — `polymarket_executor` module doesn't exist.

- [ ] **Step 4: Implement PolymarketExecutor**

Create `python-core/polymarket_executor.py`:

```python
import asyncio
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.constants import POLYGON

from kalshi_executor import ExecutorError


class PolymarketExecutor:
    """Places live orders on Polymarket via py_clob_client.

    py_clob_client is synchronous — orders run in a thread executor so they
    don't block the asyncio event loop.
    """

    def __init__(self, config: dict):
        self._config = config
        self._client: Optional[ClobClient] = None

    def _get_client(self) -> ClobClient:
        if self._client is None:
            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=self._config["polymarket_private_key"],
                chain_id=POLYGON,
                signature_type=0,
                funder=self._config.get("polymarket_wallet_address", ""),
            )
            api_creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(api_creds)
        return self._client

    def _place_sync(self, token_id: str, amount_usdc: float) -> dict:
        client = self._get_client()
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usdc,
        )
        signed_order = client.create_market_order(order_args)
        resp = client.post_order(signed_order, OrderType.FOK)
        if isinstance(resp, dict) and resp.get("errorMsg"):
            raise ExecutorError(f"Polymarket order rejected: {resp['errorMsg']}")
        return {
            "order_id": resp.get("orderID", ""),
            "status": resp.get("status", ""),
            "platform": "polymarket",
            "token_id": token_id,
            "amount_usdc": amount_usdc,
        }

    def _close_sync(self, token_id: str, amount_usdc: float) -> None:
        client = self._get_client()
        from py_clob_client.clob_types import SELL
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usdc,
            side=SELL,
        )
        signed_order = client.create_market_order(order_args)
        client.post_order(signed_order, OrderType.FOK)

    async def place_order(self, token_id: str, side: str, amount_usdc: float) -> dict:
        """Place a market BUY order on Polymarket. side param is ignored (always BUY)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._place_sync, token_id, amount_usdc)

    async def close_order(self, token_id: str, amount_usdc: float) -> None:
        """Emergency close: sell back a filled position."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._close_sync, token_id, amount_usdc)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd python-core
uv run pytest tests/test_polymarket_executor.py -v
```

Expected: all 3 tests pass.

- [ ] **Step 6: Commit**

```bash
git add python-core/pyproject.toml python-core/polymarket_executor.py python-core/tests/test_polymarket_executor.py
git commit -m "feat: PolymarketExecutor via py_clob_client SDK"
```

---

## Task 4: TwoLegExecutor — Concurrent Execution + Emergency Close

**Files:**
- Create: `python-core/two_leg_executor.py`
- Create: `python-core/tests/test_two_leg_executor.py`

Fires both legs concurrently. On partial fill, immediately calls `emergency_close` on the filled leg and writes to `emergency_positions`. Contains Kelly sizing (moved from `executor.py`).

- [ ] **Step 1: Write the failing tests**

Create `python-core/tests/test_two_leg_executor.py`:

```python
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tracker import _create_tables
from two_leg_executor import TwoLegExecutor
from kalshi_executor import ExecutorError


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_tables(conn)
    yield conn
    conn.close()


@pytest.fixture
def config():
    return {
        "dry_run": False,
        "bankroll_usdc": 500.0,
        "kelly_fraction": 0.25,
        "min_bet_usdc": 10.0,
        "max_bet_usdc": 100.0,
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
        "kalshi_api_key": "test_key",
        "kalshi_api_secret": "test_secret_32bytes_padding_here!",
        "kalshi_api_url": "https://api.elections.kalshi.com/trade-api/v2",
    }


@pytest.fixture
def cross_platform_gap():
    return {
        "pair_type": "cross_platform",
        "market_id": "test-market",
        "polymarket_price": 0.30,  # Poly YES = 0.30, so Poly NO = 0.70
        "kalshi_price": 0.22,      # Kalshi YES = 0.22
        # combined = 0.70 + 0.22 = 0.92 → 8¢ gap
        "gap_cents": 8.0,
        "confidence": "medium",
        "polymarket_token": "abc123token",
        "kalshi_ticker": "KXTEST-25DEC",
        "fee_rate": 0.02,
    }


@pytest.fixture
def internal_gap():
    return {
        "pair_type": "internal",
        "market_id": "99::tokenA-tokenB",
        "polymarket_price": 0.50,
        "kalshi_price": 0.45,
        # combined = 0.50 + 0.45 = 0.95 → 5¢ gap
        "gap_cents": 5.0,
        "confidence": "high",
        "polymarket_token": "tokenA_hex",
        "kalshi_ticker": "tokenB_hex",
        "fee_rate": 0.02,
    }


@pytest.mark.asyncio
async def test_both_legs_succeed_cross_platform(config, db, cross_platform_gap):
    poly_result = {"order_id": "poly_1", "status": "matched", "platform": "polymarket",
                   "token_id": "abc123token", "amount_usdc": 5.0}
    kalshi_result = {"order_id": "kal_1", "status": "resting", "platform": "kalshi",
                     "ticker": "KXTEST-25DEC", "count": 11}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is not None
    assert result["polymarket_order_id"] == "poly_1"
    assert result["kalshi_order_id"] == "kal_1"
    assert result["total_spent"] > 0
    assert result["gap_cents"] == 8.0


@pytest.mark.asyncio
async def test_both_legs_fail_returns_none(config, db, cross_platform_gap):
    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        MockPoly.return_value.place_order = AsyncMock(side_effect=ExecutorError("poly fail"))
        MockKalshi.return_value.place_order = AsyncMock(side_effect=ExecutorError("kalshi fail"))

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is None
    # No emergency position written when both fail
    row = db.execute("SELECT COUNT(*) FROM emergency_positions").fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_polymarket_fills_kalshi_fails_emergency_close(config, db, cross_platform_gap):
    poly_result = {"order_id": "poly_1", "status": "matched", "platform": "polymarket",
                   "token_id": "abc123token", "amount_usdc": 5.0}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockPoly.return_value.close_order = AsyncMock()
        MockKalshi.return_value.place_order = AsyncMock(side_effect=ExecutorError("kalshi fail"))

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is None
    # Emergency close was called on the polymarket filled leg
    MockPoly.return_value.close_order.assert_called_once()
    # Emergency position recorded in DB
    row = db.execute("SELECT * FROM emergency_positions WHERE order_id='poly_1'").fetchone()
    assert row is not None
    assert row["status"] == "open"


@pytest.mark.asyncio
async def test_kalshi_fills_polymarket_fails_emergency_close(config, db, cross_platform_gap):
    kalshi_result = {"order_id": "kal_1", "status": "resting", "platform": "kalshi",
                     "ticker": "KXTEST-25DEC", "count": 11}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        MockPoly.return_value.place_order = AsyncMock(side_effect=ExecutorError("poly fail"))
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)
        MockKalshi.return_value.close_order = AsyncMock()

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is None
    MockKalshi.return_value.close_order.assert_called_once()
    row = db.execute("SELECT * FROM emergency_positions WHERE order_id='kal_1'").fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_internal_pair_uses_polymarket_for_both_legs(config, db, internal_gap):
    poly_result_a = {"order_id": "poly_a", "status": "matched", "platform": "polymarket",
                     "token_id": "tokenA_hex", "amount_usdc": 5.0}
    poly_result_b = {"order_id": "poly_b", "status": "matched", "platform": "polymarket",
                     "token_id": "tokenB_hex", "amount_usdc": 4.5}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor"):
        MockPoly.return_value.place_order = AsyncMock(
            side_effect=[poly_result_a, poly_result_b]
        )

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(internal_gap, bet_size=10.0)

    assert result is not None
    assert MockPoly.return_value.place_order.call_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd python-core
uv run pytest tests/test_two_leg_executor.py -v
```

Expected: FAIL — `two_leg_executor` module doesn't exist.

- [ ] **Step 3: Implement TwoLegExecutor**

Create `python-core/two_leg_executor.py`:

```python
import asyncio
import logging
import sqlite3
from typing import Optional

from kalshi_executor import ExecutorError, KalshiExecutor
from kelly_engine import compute_arb_kelly_size
from polymarket_executor import PolymarketExecutor
from tracker import log_emergency_position

log = logging.getLogger(__name__)


class TwoLegExecutor:
    """Fires both legs of an arb trade concurrently.

    Cross-platform: Polymarket (NO or YES) + Kalshi (YES).
    Internal:       Polymarket token_a (YES) + Polymarket token_b (YES).

    On partial fill, immediately emergency-closes the filled leg and records
    the position in the emergency_positions table for manual review.
    """

    def __init__(self, config: dict, db_conn: sqlite3.Connection):
        self._config = config
        self._db = db_conn
        self._poly = PolymarketExecutor(config)
        self._kalshi = KalshiExecutor(config)

    def _compute_bet_size(self, gap: dict) -> float:
        bankroll = self._config.get("bankroll_usdc", 500.0)
        fraction = self._config.get("kelly_fraction", 0.25)
        min_bet = self._config.get("min_bet_usdc", 10.0)
        max_bet = self._config.get("max_bet_usdc", 100.0)
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
            confidence=gap.get("confidence", "medium"),
            fraction=fraction,
            max_bet_pct=0.05,
            min_bet_usdc=min_bet,
            max_bet_usdc=max_bet,
        )
        return result["bet_usdc"] if result["action"] == "BET" else min_bet

    async def execute(self, gap: dict, bet_size: Optional[float] = None) -> Optional[dict]:
        if bet_size is None:
            bet_size = self._compute_bet_size(gap)

        pair_type = gap.get("pair_type", "cross_platform")
        if pair_type == "internal":
            return await self._execute_internal(gap, bet_size)
        return await self._execute_cross_platform(gap, bet_size)

    async def _execute_cross_platform(self, gap: dict, bet_size: float) -> Optional[dict]:
        poly_price = gap["polymarket_price"]
        kalshi_price = gap["kalshi_price"]
        combined = (1.0 - poly_price) + kalshi_price
        k = bet_size / combined if combined > 0 else 0.0
        poly_amount = round(k * (1.0 - poly_price), 4)
        kalshi_count = max(1, round(k))

        poly_task = self._poly.place_order(
            token_id=gap["polymarket_token"],
            side="BUY",
            amount_usdc=poly_amount,
        )
        kalshi_task = self._kalshi.place_order(
            ticker=gap["kalshi_ticker"],
            action="buy",
            count=kalshi_count,
        )
        return await self._gather_legs(
            gap, poly_task, kalshi_task, bet_size=bet_size,
            poly_amount=poly_amount, kalshi_count=kalshi_count
        )

    async def _execute_internal(self, gap: dict, bet_size: float) -> Optional[dict]:
        poly_price = gap["polymarket_price"]
        kalshi_price = gap["kalshi_price"]
        combined = poly_price + kalshi_price
        k = bet_size / combined if combined > 0 else 0.0
        amount_a = round(k * poly_price, 4)
        amount_b = round(k * kalshi_price, 4)

        task_a = self._poly.place_order(
            token_id=gap["polymarket_token"],
            side="BUY",
            amount_usdc=amount_a,
        )
        task_b = self._poly.place_order(
            token_id=gap["kalshi_ticker"],  # token_b stored in kalshi_ticker for internal pairs
            side="BUY",
            amount_usdc=amount_b,
        )
        return await self._gather_legs(
            gap, task_a, task_b, bet_size=bet_size,
            poly_amount=amount_a, kalshi_count=None
        )

    async def _gather_legs(
        self,
        gap: dict,
        task_a,
        task_b,
        bet_size: float,
        poly_amount: float,
        kalshi_count: Optional[int],
    ) -> Optional[dict]:
        result_a, result_b = await asyncio.gather(task_a, task_b, return_exceptions=True)

        a_ok = not isinstance(result_a, Exception)
        b_ok = not isinstance(result_b, Exception)

        if a_ok and b_ok:
            fee_rate = gap.get("fee_rate", 0.04)
            combined = (
                gap["polymarket_price"] + gap["kalshi_price"]
                if gap.get("pair_type") == "internal"
                else (1.0 - gap["polymarket_price"]) + gap["kalshi_price"]
            )
            k = bet_size / combined if combined > 0 else 0.0
            fee = fee_rate * bet_size
            expected_profit = round(k - bet_size - fee, 4)
            return {
                "polymarket_order_id": result_a.get("order_id", ""),
                "kalshi_order_id": result_b.get("order_id", ""),
                "total_spent": round(bet_size, 4),
                "gap_cents": gap.get("gap_cents", 0.0),
                "expected_profit": expected_profit,
                "dry_run": False,
            }

        if not a_ok and not b_ok:
            log.warning(
                "Both legs failed for %s — a: %s | b: %s",
                gap["market_id"], result_a, result_b,
            )
            return None

        # Partial fill — emergency close the filled leg
        filled_result = result_a if a_ok else result_b
        failed_error = result_b if a_ok else result_a
        log.error(
            "PARTIAL FILL on %s — filled: %s | failed: %s — emergency closing",
            gap["market_id"], filled_result.get("order_id"), failed_error,
        )
        await self._emergency_close(filled_result, gap)
        return None

    async def _emergency_close(self, filled: dict, gap: dict) -> None:
        platform = filled.get("platform", "")
        try:
            if platform == "polymarket":
                await self._poly.close_order(
                    token_id=filled["token_id"],
                    amount_usdc=filled["amount_usdc"],
                )
            elif platform == "kalshi":
                await self._kalshi.close_order(
                    ticker=filled["ticker"],
                    count=filled["count"],
                )
            status = "closed_auto"
        except Exception as e:
            log.error("Emergency close FAILED for %s: %s — REQUIRES MANUAL ACTION", filled.get("order_id"), e)
            status = "open"

        log_emergency_position(self._db, {
            "market_id": gap.get("market_id", ""),
            "platform": platform,
            "order_id": filled.get("order_id", ""),
            "side": filled.get("ticker") or filled.get("token_id", ""),
            "amount_usdc": filled.get("amount_usdc", 0.0),
        })
        # Update status if auto-close succeeded
        if status == "closed_auto":
            self._db.execute(
                "UPDATE emergency_positions SET status='closed_auto', closed_at=datetime('now') "
                "WHERE order_id=?",
                (filled.get("order_id", ""),),
            )
            self._db.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd python-core
uv run pytest tests/test_two_leg_executor.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add python-core/two_leg_executor.py python-core/tests/test_two_leg_executor.py
git commit -m "feat: TwoLegExecutor with concurrent legs and emergency close"
```

---

## Task 5: Backfill — Add fee_rate per pair + Merge Cross + Internal

**Files:**
- Modify: `scripts/backfill_matches.py`

The existing script already fetches both APIs and runs the matcher. Two things to add:
1. Attach `fee_rate` to each cross-platform pair from the Gamma API `feeSchedule.rate` field.
2. In cross-platform mode, also generate and include internal pairs (so the bot has both).

- [ ] **Step 1: Build fee_rate lookup map from poly_markets**

Open `scripts/backfill_matches.py`. After `fetch_polymarket_markets` returns, build a lookup:

Find the line `kalshi_mode = len(kalshi_markets) > 0` and add before it:

```python
    # Build fee_rate lookup: gamma_id → fee_rate
    # feeSchedule.rate is the taker fee (0.04 = 4% for politics, 0.02 = 2% for others)
    fee_rate_by_gamma_id: dict[str, float] = {}
    for m in poly_markets:
        gamma_id = str(m.get("id", ""))
        schedule = m.get("feeSchedule") or {}
        rate = float(schedule.get("rate", 0.04))
        if gamma_id:
            fee_rate_by_gamma_id[gamma_id] = rate
```

- [ ] **Step 2: Attach fee_rate to cross-platform pairs in pairs_entries**

Find the `for pair in pairs:` loop that builds `pairs_entries`. Change the `if pair.pair_type == "cross_platform":` block to also store `fee_rate`:

```python
    pairs_entries = []
    for pair in pairs:
        entry = {
            "pair_type": pair.pair_type,
            "token_a": pair.token_a,
            "token_b": pair.token_b,
            "market_id": pair.market_id,
            "confidence": pair.confidence,
            "match_method": pair.match_method,
            "gamma_id_a": pair.gamma_id_a,
            "gamma_id_b": pair.gamma_id_b,
            "outcome_count": pair.outcome_count,
        }
        if pair.pair_type == "cross_platform":
            entry["polymarket_slug"] = pair.polymarket_slug
            entry["kalshi_ticker"] = pair.kalshi_ticker
            # Attach real fee rate from Gamma API (defaults to 4% if not found)
            entry["fee_rate"] = fee_rate_by_gamma_id.get(pair.gamma_id_a, 0.04)
        else:
            # Internal pairs: Polymarket charges politics fees (4%) on most negRisk markets
            entry["fee_rate"] = fee_rate_by_gamma_id.get(pair.gamma_id_a, 0.04)
        pairs_entries.append(entry)
```

- [ ] **Step 3: In cross-platform mode, also include internal pairs**

Find where `pairs` is assigned based on `kalshi_mode`. Replace:

```python
    if kalshi_mode:
        pairs = matcher.match(liquid_markets, kalshi_markets)
    else:
        print("  Kalshi returned 0 markets — falling back to internal negRisk pairs")
        pairs = matcher.create_internal_pairs(liquid_markets, full_markets=poly_markets)
```

With:

```python
    if kalshi_mode:
        cross_pairs = matcher.match(liquid_markets, kalshi_markets)
        internal_pairs = matcher.create_internal_pairs(liquid_markets, full_markets=poly_markets)
        pairs = cross_pairs + internal_pairs
        print(f"  Cross-platform: {len(cross_pairs)} pairs | Internal fallback: {len(internal_pairs)} pairs")
    else:
        print("  Kalshi returned 0 markets — falling back to internal negRisk pairs only")
        pairs = matcher.create_internal_pairs(liquid_markets, full_markets=poly_markets)
```

- [ ] **Step 4: Verify backfill produces fee_rate field**

Run the backfill against live APIs:

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
python scripts/backfill_matches.py
```

Expected output includes:
```
Cross-platform: N pairs | Internal fallback: M pairs
Wrote X pairs to config/markets.json
```

Then verify fee_rate is in the JSON:
```bash
python3 -c "
import json
data = json.load(open('config/markets.json'))
pairs = data['pairs']
cross = [p for p in pairs if p.get('pair_type') == 'cross_platform']
print('Cross-platform pairs:', len(cross))
if cross:
    print('Sample fee_rate:', cross[0].get('fee_rate'))
    print('Sample pair:', json.dumps(cross[0], indent=2))
"
```

Expected: `fee_rate` field present on cross-platform pairs (0.04 for politics markets).

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_matches.py config/markets.json
git commit -m "feat(backfill): attach fee_rate per pair, include both cross+internal in cross mode"
```

---

## Task 6: Fee Rate + Per-Pair-Type Gap Threshold in Detector

**Files:**
- Modify: `python-core/detector.py`

Two changes: (1) read `gap["fee_rate"]` instead of global config rate, (2) apply a higher minimum gap threshold for internal pairs (8¢) vs cross-platform (5¢).

- [ ] **Step 1: Write the failing tests**

Open `python-core/tests/test_detector.py` and add:

```python
def test_cross_platform_gap_uses_per_pair_fee_rate(db):
    config = {
        "min_gap_cents": 5, "max_gap_cents": 30,
        "ev_taker_fee_rate": 0.02,  # global default (should be ignored)
        "ev_min_cents": 1.0, "ev_slippage_cents": 0.5,
        "max_daily_loss_usdc": 50.0, "max_open_positions": 999_999,
        "markets_json": "config/markets.json",
    }
    detector = GapDetector(config, db)
    # 6¢ gap, 4% fee: net EV = 6 - (0.04*0.94*100) - 0.5 = 6 - 3.76 - 0.5 = 1.74¢ → PASS at ev_min=1.0
    gap = {
        "market_id": "test-market", "polymarket_price": 0.30, "kalshi_price": 0.64,
        "gap_cents": 6.0, "confidence": "medium", "pair_type": "cross_platform",
        "polymarket_token": "tok", "kalshi_ticker": "TKR",
        "fee_rate": 0.04,  # per-pair fee rate (4% politics)
    }
    # Seed history so stability check passes
    for _ in range(3):
        detector._history["test-market"].append(6.0)
    ok, reason = detector.validate(gap)
    assert ok, reason


def test_internal_gap_requires_higher_minimum(db):
    config = {
        "min_gap_cents": 5, "max_gap_cents": 30,
        "ev_taker_fee_rate": 0.02,
        "ev_min_cents": 1.0, "ev_slippage_cents": 0.5,
        "max_daily_loss_usdc": 50.0, "max_open_positions": 999_999,
        "markets_json": "config/markets.json",
        "internal_min_gap_cents": 8.0,
    }
    detector = GapDetector(config, db)
    # 6¢ gap on internal pair — should be rejected because internal min is 8¢
    gap = {
        "market_id": "99::aaa-bbb", "polymarket_price": 0.50, "kalshi_price": 0.44,
        "gap_cents": 6.0, "confidence": "high", "pair_type": "internal",
        "polymarket_token": "tokenA", "kalshi_ticker": "tokenB",
        "fee_rate": 0.04, "outcome_count": 2,
    }
    # Seed outcome_count in market_pairs
    db.execute(
        "INSERT OR IGNORE INTO market_pairs (token_a, token_b, outcome_count) VALUES (?,?,?)",
        ("tokenA", "tokenB", 2)
    )
    db.commit()
    for _ in range(3):
        detector._history["99::aaa-bbb"].append(6.0)
    ok, reason = detector.validate(gap)
    assert not ok
    assert "8.0" in reason
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd python-core
uv run pytest tests/test_detector.py::test_cross_platform_gap_uses_per_pair_fee_rate tests/test_detector.py::test_internal_gap_requires_higher_minimum -v
```

Expected: FAIL.

- [ ] **Step 3: Update detector.py**

Open `python-core/detector.py`. Find the block starting with `# Check 1b: EV net must exceed minimum threshold` (around line 88). Replace:

```python
        # Check 1b: EV net must exceed minimum threshold after fees + slippage
        taker_fee_rate = self.config.get("ev_taker_fee_rate", 0.02)
        slippage_cents = self.config.get("ev_slippage_cents", 0.5)
        ev_min_cents = self.config.get("ev_min_cents", 1.0)
        arb_ev = calculate_arb_ev(combined, taker_fee_rate, slippage_cents)
        if arb_ev["ev_net_cents"] < ev_min_cents:
```

With:

```python
        # Check 1b: Per-pair-type minimum gap threshold
        # Internal pairs have a higher bar (negRisk mechanics more complex)
        if pair_type == "internal":
            min_gap_for_type = self.config.get("internal_min_gap_cents", 8.0)
        else:
            min_gap_for_type = self.config.get("cross_platform_min_gap_cents", 5.0)
        if gap_cents < min_gap_for_type:
            return False, f"Gap {gap_cents:.1f}¢ below {pair_type} minimum {min_gap_for_type:.1f}¢"

        # Check 1c: EV net must exceed minimum threshold after fees + slippage
        # Use per-pair fee_rate from markets.json; fall back to 4% (conservative)
        taker_fee_rate = gap.get("fee_rate", self.config.get("ev_taker_fee_rate", 0.04))
        slippage_cents = self.config.get("ev_slippage_cents", 0.5)
        ev_min_cents = self.config.get("ev_min_cents", 1.0)
        arb_ev = calculate_arb_ev(combined, taker_fee_rate, slippage_cents)
        if arb_ev["ev_net_cents"] < ev_min_cents:
```

- [ ] **Step 4: Run all detector tests**

```bash
cd python-core
uv run pytest tests/test_detector.py -v
```

Expected: all tests pass including the 2 new ones.

- [ ] **Step 5: Commit**

```bash
git add python-core/detector.py python-core/tests/test_detector.py
git commit -m "feat(detector): per-pair fee_rate, cross_platform=5c / internal=8c min gap"
```

---

## Task 7: Reconciler Fix — Real Profit from gap_cents

**Files:**
- Modify: `python-core/reconciler.py`
- Modify: `python-core/tests/test_reconciler.py`

Replace `actual_profit = amount_usdc * 0.08` with the correct arb formula using `gap_cents` stored in the trade row.

- [ ] **Step 1: Write the failing tests**

Open `python-core/tests/test_reconciler.py` and replace the existing `compute_actual_profit` tests:

```python
from reconciler import compute_actual_profit, ResolutionResult


def test_compute_profit_yes_resolution_cross_platform():
    # Cross-platform: bought Poly NO + Kalshi YES
    # YES wins → Kalshi YES wins, Poly NO loses
    # combined = (1-0.30) + 0.22 = 0.92, gap_cents=8.0
    # k = 10 / 0.92 = 10.87, gross = 10.87 - 10 = 0.87
    # fee = 0.02 * 10 = 0.20, net = 0.87 - 0.20 = 0.67
    result = compute_actual_profit(
        poly_side="NO",
        kalshi_side="YES",
        resolution="YES",
        amount_usdc=10.0,
        gap_cents=8.0,
        fee_rate=0.02,
    )
    assert isinstance(result, ResolutionResult)
    assert result.status == "profit"
    assert abs(result.actual_profit - 0.67) < 0.02


def test_compute_profit_no_resolution_cross_platform():
    # NO wins → Poly NO wins, Kalshi YES loses
    # Same formula — arb pays regardless of which side wins
    result = compute_actual_profit(
        poly_side="NO",
        kalshi_side="YES",
        resolution="NO",
        amount_usdc=10.0,
        gap_cents=8.0,
        fee_rate=0.02,
    )
    assert result.status == "profit"
    assert abs(result.actual_profit - 0.67) < 0.02


def test_compute_profit_internal_pair():
    # Internal: both YES tokens, combined=0.95, gap_cents=5.0
    # k = 10 / 0.95 = 10.53, gross = 0.53, fee = 0.04*10 = 0.40, net = 0.13
    result = compute_actual_profit(
        poly_side="YES",
        kalshi_side="YES",
        resolution="YES",
        amount_usdc=10.0,
        gap_cents=5.0,
        fee_rate=0.04,
    )
    assert result.status == "profit"
    assert result.actual_profit > 0
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd python-core
uv run pytest tests/test_reconciler.py::test_compute_profit_yes_resolution_cross_platform tests/test_reconciler.py::test_compute_profit_no_resolution_cross_platform tests/test_reconciler.py::test_compute_profit_internal_pair -v
```

Expected: FAIL — current function signature doesn't accept `gap_cents` or `fee_rate`.

- [ ] **Step 3: Fix compute_actual_profit in reconciler.py**

Open `python-core/reconciler.py`. Replace the entire `compute_actual_profit` function:

```python
def compute_actual_profit(
    poly_side: str,
    kalshi_side: str,
    resolution: str,
    amount_usdc: float,
    gap_cents: float,
    fee_rate: float = 0.04,
) -> ResolutionResult:
    """Compute actual profit for a resolved two-leg arb trade.

    For guaranteed arb, the net profit is the same regardless of which leg wins.
    gross = (amount_usdc / combined) - amount_usdc
    net   = gross - fee_rate * amount_usdc
    """
    combined = 1.0 - gap_cents / 100.0
    k = amount_usdc / combined if combined > 0 else 0.0
    gross = k - amount_usdc
    fee = fee_rate * amount_usdc
    actual_profit = round(gross - fee, 4)
    status = "profit" if actual_profit >= 0 else "loss"
    return ResolutionResult(trade_id=0, status=status, actual_profit=actual_profit)
```

- [ ] **Step 4: Fix _reconcile_once to pass gap_cents and fee_rate**

In the same file, update `_reconcile_once` to read `gap_cents` and `fee_rate` from the trade row. Find the `result = compute_actual_profit(...)` call and replace it:

```python
            result = compute_actual_profit(
                poly_side=trade.get("polymarket_side", "NO"),
                kalshi_side=trade.get("kalshi_side", "YES"),
                resolution=resolution,
                amount_usdc=trade["amount_usdc"],
                gap_cents=float(trade.get("gap_cents") or 8.0),
                fee_rate=float(trade.get("fee_rate") or 0.04),
            )
```

Also add `fee_rate` to the `get_open_live_trades` query in `tracker.py` — find that function and make sure it selects `gap_cents`:

In `tracker.py`, find `get_open_live_trades` and ensure the SELECT includes `t.gap_cents`:
```python
def get_open_live_trades(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT t.id, t.polymarket_side, t.kalshi_side, t.amount_usdc,
                  t.gap_cents, t.expected_profit, t.opened_at,
                  g.market_id, g.polymarket_price, g.kalshi_price
           FROM trades t
           JOIN gaps g ON t.gap_id = g.id
           WHERE t.status = 'open' AND t.dry_run = 0"""
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: Run all reconciler tests**

```bash
cd python-core
uv run pytest tests/test_reconciler.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add python-core/reconciler.py python-core/tracker.py python-core/tests/test_reconciler.py
git commit -m "fix(reconciler): real profit from gap_cents, not hardcoded 8%"
```

---

## Task 8: Rust Bridge → One-Directional

**Files:**
- Modify: `rust-core/src/bridge.rs`
- Modify: `rust-core/src/main.rs`
- Modify: `rust-core/src/executor.rs`

Rust no longer receives execute commands from Python. The bridge writes gap events to stdout only. Remove the stdin reader, the order channel, and the executor spawner from `main.rs`.

- [ ] **Step 1: Simplify bridge.rs**

Open `rust-core/src/bridge.rs`. Replace the entire file content:

```rust
use anyhow::Result;
use log::info;
use tokio::io::AsyncWriteExt;

use crate::types::Gap;

pub async fn run(gap_rx: crossbeam_channel::Receiver<Gap>) -> Result<()> {
    let stdout = tokio::io::stdout();
    let mut writer = tokio::io::BufWriter::new(stdout);

    info!("Bridge started — writing gap events to stdout");

    loop {
        while let Ok(gap) = gap_rx.try_recv() {
            let json = serde_json::to_string(&gap)?;
            writer.write_all(json.as_bytes()).await?;
            writer.write_all(b"\n").await?;
            writer.flush().await?;
        }
        tokio::time::sleep(tokio::time::Duration::from_millis(5)).await;
    }
}
```

- [ ] **Step 2: Simplify main.rs — remove executor and order channels**

Open `rust-core/src/main.rs`. Replace the entire file:

```rust
use arb::{bridge, comparator, fetcher, types};

use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use anyhow::Result;
use log::info;

use types::{AppConfig, MarketPair};

#[tokio::main]
async fn main() -> Result<()> {
    dotenv::dotenv().ok();
    env_logger::init();

    let config = AppConfig::from_env()?;
    info!("Arb bot starting. DRY_RUN={}", config.dry_run);

    let pairs = load_market_pairs();
    info!("Loaded {} market pairs", pairs.len());

    let price_map: Arc<RwLock<HashMap<String, types::Price>>> =
        Arc::new(RwLock::new(HashMap::new()));

    let (gap_tx, gap_rx) = crossbeam_channel::bounded::<types::Gap>(1000);

    // Build token → gamma_id map for REST price polling
    let token_to_gamma_id: HashMap<String, String> = pairs
        .iter()
        .flat_map(|p| {
            let mut entries = vec![(p.token_a.clone(), p.gamma_id_a.clone())];
            if p.pair_type == types::PairType::Internal {
                entries.push((p.token_b.clone(), p.gamma_id_b.clone()));
            }
            entries
        })
        .filter(|(tok, gid)| !tok.is_empty() && !gid.is_empty())
        .collect();

    let kalshi_pairs: Vec<MarketPair> = pairs
        .iter()
        .filter(|p| p.pair_type == types::PairType::CrossPlatform)
        .cloned()
        .collect();

    // Spawn Polymarket REST poller
    let poly_map = Arc::clone(&price_map);
    let gamma_url = config.polymarket_gamma_url.clone();
    tokio::spawn(async move {
        if let Err(e) = fetcher::polymarket::run(gamma_url, token_to_gamma_id, poly_map).await {
            log::error!("Polymarket fetcher error: {e}");
        }
    });

    // Spawn Kalshi REST poller
    let kalshi_map = Arc::clone(&price_map);
    let kalshi_api_url = config.kalshi_api_url.clone();
    let kalshi_key = config.kalshi_api_key.clone();
    let kalshi_secret = config.kalshi_api_secret.clone();
    tokio::spawn(async move {
        if let Err(e) =
            fetcher::kalshi::run(kalshi_api_url, kalshi_key, kalshi_secret, kalshi_pairs, kalshi_map)
                .await
        {
            log::error!("Kalshi fetcher error: {e}");
        }
    });

    // Spawn comparator
    let comp_map = Arc::clone(&price_map);
    let comp_pairs = pairs.clone();
    let comp_config = config.clone();
    tokio::spawn(async move {
        if let Err(e) = comparator::run(comp_config, comp_pairs, comp_map, gap_tx).await {
            log::error!("Comparator error: {e}");
        }
    });

    // Bridge: write gaps to stdout (one-directional — Python handles all execution)
    bridge::run(gap_rx).await?;

    Ok(())
}

fn load_market_pairs() -> Vec<MarketPair> {
    let path = std::env::var("MARKETS_JSON")
        .unwrap_or_else(|_| "config/markets.json".to_string());

    let data = match std::fs::read_to_string(&path) {
        Ok(d) => d,
        Err(_) => return vec![],
    };

    let val: serde_json::Value = match serde_json::from_str(&data) {
        Ok(v) => v,
        Err(_) => return vec![],
    };

    if let Some(pairs) = val["pairs"].as_array() {
        return pairs
            .iter()
            .filter_map(|p| {
                let token_a = p["token_a"].as_str()?;
                let token_b = p["token_b"].as_str()?;
                let market_id = p["market_id"].as_str().unwrap_or(token_a);
                let pair_type = match p["pair_type"].as_str().unwrap_or("cross_platform") {
                    "internal" => types::PairType::Internal,
                    _ => types::PairType::CrossPlatform,
                };
                Some(types::MarketPair {
                    pair_type,
                    token_a: token_a.to_string(),
                    token_b: token_b.to_string(),
                    market_id: market_id.to_string(),
                    gamma_id_a: p["gamma_id_a"].as_str().unwrap_or("").to_string(),
                    gamma_id_b: p["gamma_id_b"].as_str().unwrap_or("").to_string(),
                })
            })
            .collect();
    }

    vec![]
}
```

- [ ] **Step 3: Simplify executor.rs — remove live_execute**

Open `rust-core/src/executor.rs`. Replace the entire file:

```rust
use anyhow::Result;
use log::info;
use uuid::Uuid;

use crate::types::{ExecuteCommand, OrderPlaced};

// live_execute removed — all order placement is handled by Python (py_clob_client + HMAC).
// dry_run_execute kept for testing and simulation.

pub fn dry_run_execute(cmd: ExecuteCommand) -> Result<OrderPlaced> {
    let poly_order_id = format!("dry_{}", &Uuid::new_v4().to_string()[..8]);
    let kalshi_order_id = format!("dry_{}", &Uuid::new_v4().to_string()[..8]);

    let total_spent = cmd.polymarket_amount + cmd.kalshi_amount;
    let combined = 1.0 - cmd.gap_cents / 100.0;
    let k = if combined > 0.0 { total_spent / combined } else { 0.0 };
    let fee = cmd.taker_fee_rate * total_spent;
    let expected_profit = k - total_spent - fee;

    info!(
        "DRY RUN | Poly {} ${:.2} | Kalshi {} ${:.2} | Gap {:.1}¢ | Fee ${:.2} | Net: +${:.2}",
        cmd.polymarket_side, cmd.polymarket_amount,
        cmd.kalshi_side, cmd.kalshi_amount,
        cmd.gap_cents, fee, expected_profit
    );

    Ok(OrderPlaced {
        event: "order_placed".to_string(),
        polymarket_order_id: poly_order_id,
        kalshi_order_id,
        total_spent,
        expected_profit,
        dry_run: true,
    })
}
```

- [ ] **Step 4: Rebuild Rust**

```bash
cd rust-core
cargo build --release 2>&1 | tail -5
```

Expected: `Finished release profile` with no errors.

- [ ] **Step 5: Commit**

```bash
git add rust-core/src/bridge.rs rust-core/src/main.rs rust-core/src/executor.rs
git commit -m "refactor(rust): one-directional bridge, remove live_execute — Python handles all orders"
```

---

## Task 9: main.py Wiring — Startup Backfill, TwoLegExecutor, fee_rate_map

**Files:**
- Modify: `python-core/main.py`
- Modify: `python-core/executor.py` (delete)
- Modify: `config/.env.example`

Wire TwoLegExecutor into the gap handler. Add startup backfill age check. Build fee_rate_map from loaded pairs. Remove all Rust stdin writes.

- [ ] **Step 1: Update .env.example**

Open `config/.env.example` and add under `# --- GAP THRESHOLDS ---`:

```env
CROSS_PLATFORM_MIN_GAP_CENTS=5    # minimum gap for Poly vs Kalshi trades
INTERNAL_MIN_GAP_CENTS=8          # minimum gap for internal Poly negRisk trades
```

Also ensure these exist:
```env
POLYMARKET_PRIVATE_KEY=           # your wallet private key (0x...)
POLYMARKET_WALLET_ADDRESS=        # your wallet address (0x...)
```

- [ ] **Step 2: Update CONFIG dict in main.py**

Open `python-core/main.py`. Find the `CONFIG = { ... }` block and add:

```python
    "cross_platform_min_gap_cents": float(os.getenv("CROSS_PLATFORM_MIN_GAP_CENTS", "5")),
    "internal_min_gap_cents": float(os.getenv("INTERNAL_MIN_GAP_CENTS", "8")),
    "polymarket_private_key": os.getenv("POLYMARKET_PRIVATE_KEY", ""),
    "polymarket_wallet_address": os.getenv("POLYMARKET_WALLET_ADDRESS", ""),
```

- [ ] **Step 3: Add startup backfill age check**

At the top of `main.py`, add this import after existing imports:

```python
import subprocess
from datetime import datetime, timezone, timedelta
```

At the start of `async def main():`, before the `db_conn = tracker.init_db(...)` line, add:

```python
    # Run backfill if markets.json is >24 hours old or missing cross-platform pairs
    markets_path = Path(CONFIG["markets_json"])
    needs_backfill = False
    if not markets_path.exists():
        needs_backfill = True
    else:
        mtime = datetime.fromtimestamp(markets_path.stat().st_mtime, tz=timezone.utc)
        if datetime.now(timezone.utc) - mtime > timedelta(hours=24):
            needs_backfill = True
        else:
            _data = json.loads(markets_path.read_text())
            has_cross = any(p.get("pair_type") == "cross_platform" for p in _data.get("pairs", []))
            if not has_cross:
                needs_backfill = True

    if needs_backfill:
        notifier.logger.info("markets.json is stale or missing cross-platform pairs — running backfill...")
        result = subprocess.run(
            ["python", "scripts/backfill_matches.py"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            notifier.logger.warning(f"Backfill failed: {result.stderr[:200]}")
        else:
            notifier.logger.info("Backfill complete")
```

- [ ] **Step 4: Build fee_rate_map from loaded pairs**

After the pairs are loaded (the block that sets `high`, `medium`, `low`), add:

```python
    # fee_rate_map: market_id → fee_rate — enriches gap events from Rust with per-pair fee
    fee_rate_map: dict[str, float] = {
        p.get("market_id", p.get("token_a", "")): p.get("fee_rate", 0.04)
        for p in pairs
    }
```

- [ ] **Step 5: Import TwoLegExecutor and replace Executor**

At the top of `main.py`, replace:

```python
from executor import Executor
```

With:

```python
from two_leg_executor import TwoLegExecutor
```

In `async def main():`, replace:

```python
    executor = Executor(CONFIG, rust_process.stdin, stdout_queue)
```

With:

```python
    executor = TwoLegExecutor(CONFIG, db_conn)
```

- [ ] **Step 6: Update _handle_gap to use TwoLegExecutor and fee_rate_map**

Find `async def _handle_gap(...)`. The function signature and call to `executor.execute` need to change.

Replace the function signature from:
```python
async def _handle_gap(gap: dict, detector: GapDetector, executor: Executor, db_conn, stdout_queue, bayes_engine: BayesEngine):
```

To:
```python
async def _handle_gap(gap: dict, detector: GapDetector, executor: TwoLegExecutor, db_conn, stdout_queue, bayes_engine: BayesEngine, fee_rate_map: dict):
```

At the start of `_handle_gap`, after `market_id = gap["market_id"]`, add:

```python
    # Attach per-pair fee_rate so detector and EV gate use the correct rate
    gap["fee_rate"] = fee_rate_map.get(market_id, 0.04)
```

Replace the confirmation/trade building block (lines ~188-212) with:

```python
    confirmation = await executor.execute(gap)

    if not confirmation:
        notifier.logger.warning(f"Execution failed for {market_id} — gap logged, no trade")
        return

    pair_type = gap.get("pair_type", "cross_platform")
    poly_side = "YES" if pair_type == "internal" else "NO"
    trade = {
        "gap_id": gap_id,
        "polymarket_order_id": confirmation.get("polymarket_order_id"),
        "kalshi_order_id": confirmation.get("kalshi_order_id"),
        "polymarket_side": poly_side,
        "kalshi_side": "YES",
        "polymarket_amount": confirmation.get("total_spent", 0) / 2,
        "kalshi_amount": confirmation.get("total_spent", 0) / 2,
        "amount_usdc": confirmation.get("total_spent"),
        "gap_cents": confirmation.get("gap_cents"),
        "expected_profit": confirmation.get("expected_profit"),
        "dry_run": CONFIG.get("dry_run", True),
    }
```

- [ ] **Step 7: Update the _handle_gap call site in _read_stdout**

Find where `_handle_gap` is called (in `_read_stdout`):

```python
            asyncio.create_task(_handle_gap(event, detector, executor, db_conn, stdout_queue, bayes_engine))
```

Replace with:

```python
            asyncio.create_task(_handle_gap(event, detector, executor, db_conn, stdout_queue, bayes_engine, fee_rate_map))
```

Also update the `asyncio.create_task(_read_stdout(...))` call to pass `fee_rate_map` as well, and update `_read_stdout`'s signature to accept and forward it.

- [ ] **Step 8: Remove stdout_queue dependency — Rust no longer sends confirmations**

Since Python no longer waits for Rust confirmations, the `stdout_queue` is only needed for gap events now. The `_read_stdout` function should no longer route `order_placed` events to the queue. Find and remove:

```python
        elif event_type == "order_placed":
            await stdout_queue.put(event)
```

- [ ] **Step 9: Delete executor.py**

```bash
rm python-core/executor.py
```

- [ ] **Step 10: Run full test suite**

```bash
cd python-core
uv run pytest tests/ -v
```

Expected: all tests pass (some tests that imported `executor.py` may need updating — fix any import errors).

- [ ] **Step 11: Commit**

```bash
git add python-core/main.py python-core/pyproject.toml config/.env.example
git rm python-core/executor.py
git commit -m "feat: wire TwoLegExecutor into main, startup backfill check, fee_rate_map"
```

---

## Task 10: Smoke Test — Verify Cross-Platform Gaps in Dry-Run

This is a manual verification step, not automated. Run the bot in dry-run mode and confirm the new architecture works end-to-end.

- [ ] **Step 1: Ensure markets.json has cross-platform pairs**

```bash
python3 -c "
import json
data = json.load(open('config/markets.json'))
pairs = data['pairs']
cross = [p for p in pairs if p.get('pair_type') == 'cross_platform']
internal = [p for p in pairs if p.get('pair_type') == 'internal']
print(f'Cross-platform: {len(cross)}, Internal: {len(internal)}')
if cross:
    print('Sample:', cross[0]['market_id'], '| fee_rate:', cross[0].get('fee_rate'))
"
```

Expected: cross-platform > 0.

- [ ] **Step 2: Confirm .env has DRY_RUN=true**

```bash
grep DRY_RUN .env
```

Expected: `DRY_RUN=true`

- [ ] **Step 3: Start the bot**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
python python-core/main.py
```

Expected within 60 seconds:
```
INFO | Running in CROSS PLATFORM mode
INFO | N pairs loaded (X cross-platform, Y internal)
INFO | Gap detected: <market_id> | pair_type: cross_platform | gap: X.Xc
INFO | Dry-run trade logged: #N
```

- [ ] **Step 4: Verify cross-platform trades appear in DB**

```bash
sqlite3 data/trades.db "
SELECT pair_type_from_gap, COUNT(*) as count
FROM trades t
JOIN gaps g ON t.gap_id = g.id
WHERE t.opened_at >= date('now')
GROUP BY 1;
"
```

Or simpler:

```bash
sqlite3 data/trades.db "SELECT COUNT(*) FROM trades WHERE opened_at >= date('now') AND dry_run=1;"
```

Expected: trades count increasing.

- [ ] **Step 5: Verify no Rust execute commands needed**

Check that Python no longer sends anything to Rust stdin by confirming there's no `rust_process.stdin.write` in main.py:

```bash
grep -n "rust_process.stdin" python-core/main.py
```

Expected: no output (no stdin writes).

- [ ] **Step 6: Final commit — rebuild Rust if needed**

```bash
cd rust-core && cargo build --release && cd ..
git add -A
git commit -m "feat: live trading execution layer complete — 15% → 80% readiness"
```

---

## Environment Variables Required for Live Trading

Before switching `DRY_RUN=false`, ensure these are set in `.env`:

```env
# Polymarket (required for live orders)
POLYMARKET_PRIVATE_KEY=0x<your_wallet_private_key>
POLYMARKET_WALLET_ADDRESS=0x<your_wallet_address>

# Kalshi (required for live orders)
KALSHI_API_KEY=<your_kalshi_api_key>
KALSHI_API_SECRET=<your_kalshi_api_secret>

# Start conservative
MAX_BET_USDC=10
MIN_BET_USDC=10
DRY_RUN=false
```

Do **not** go live until Task 10 smoke test passes cleanly in dry-run mode showing cross-platform gaps.
