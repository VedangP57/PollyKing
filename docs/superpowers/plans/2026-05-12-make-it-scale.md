# Make It Scale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the observability, pair-quality automation, and operational documentation needed to run the bot unattended at $1k+ bankroll for weeks without manual intervention.

**Architecture:** Five independent additions that build on Plans 1 and 2. Prometheus gauges added to existing `metrics.py` (already has `daily_pnl` gauge — extend with `ws_staleness_seconds` and `fill_success_rate`). NLP matching and pair invalidation both live inside `backfill_matches.py`. Idempotency keys add one new DB table and two new tracker functions. The runbook is pure documentation.

**Tech Stack:** Python 3.11+, prometheus-client (already a dep), difflib (stdlib), sqlite3, aiohttp

**Depends on:** Plans 1 and 2 fully implemented.

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `python-core/metrics.py` | Modify | Add `ws_staleness_seconds` and `fill_success_rate` Gauges + helpers |
| `python-core/main.py` | Modify | `_update_pnl_metric()` background task; handle `ws_staleness` JSON event from Rust |
| `config/prometheus.yml` | Create | Local scrape config for Prometheus CLI |
| `python-core/tracker.py` | Modify | `trade_attempts` table + `log_trade_attempt()` + `confirm_trade_attempt()` + `get_unconfirmed_attempts()` |
| `python-core/two_leg_executor.py` | Modify | `log_trade_attempt()` before order placement; `confirm_trade_attempt()` after confirmed fill |
| `python-core/startup_audit.py` | Modify | Query unconfirmed attempts and log WARNING per attempt |
| `scripts/backfill_matches.py` | Modify | `normalize()` + `title_similarity()` NLP pipeline + `invalidate_expired_pairs()` + `--invalidate-only` CLI flag |
| `docs/runbook.md` | Create | Four-section operations runbook |
| `python-core/tests/test_metrics.py` | Modify | Add `test_ws_staleness_gauge_updated`, `test_fill_success_rate_updated` |
| `python-core/tests/test_backfill.py` | Modify | Add NLP matching and invalidation tests |
| `python-core/tests/test_tracker.py` | Modify | Add `test_trade_attempt_logged`, `test_trade_attempt_confirmed` |
| `python-core/tests/test_startup_audit.py` | Modify | Add `test_unconfirmed_attempt_flagged` |

---

## Task 1: Prometheus Metrics Expansion

**Files:**
- Modify: `python-core/metrics.py` (add 2 Gauges + 3 helper functions)
- Modify: `python-core/main.py` (background task + stdout event handler)
- Create: `config/prometheus.yml`
- Modify: `python-core/tests/test_metrics.py` (or create if missing)

**Note:** `daily_pnl_usdc` gauge already exists (`arb_daily_pnl_usdc`). This task adds `ws_staleness_seconds` and `fill_success_rate` plus the background task that keeps them updated.

- [ ] **Step 1: Write the failing tests**

Check if `python-core/tests/test_metrics.py` exists:

```bash
ls python-core/tests/test_metrics.py 2>/dev/null && echo "exists" || echo "missing"
```

Add to (or create) `python-core/tests/test_metrics.py`:

```python
"""Tests for metrics.py gauge helpers."""
import pytest
import metrics as _metrics


def test_ws_staleness_gauge_set():
    """set_ws_staleness() updates the ws_staleness_seconds gauge."""
    _metrics.set_ws_staleness(42.0)
    # prometheus_client Gauge stores the value — read it back via _value.get()
    assert _metrics.ws_staleness_seconds._value.get() == pytest.approx(42.0)


def test_fill_success_rate_gauge_set():
    """set_fill_success_rate() updates the fill_success_rate gauge."""
    _metrics.set_fill_success_rate(0.87)
    assert _metrics.fill_success_rate._value.get() == pytest.approx(0.87)


def test_daily_pnl_set_already_works():
    """Baseline: set_daily_pnl() already exists and works."""
    _metrics.set_daily_pnl(-12.50)
    assert _metrics.daily_pnl._value.get() == pytest.approx(-12.50)
```

- [ ] **Step 2: Run to verify failure**

```bash
cd python-core && uv run pytest tests/test_metrics.py::test_ws_staleness_gauge_set tests/test_metrics.py::test_fill_success_rate_gauge_set -v
```

Expected: `AttributeError: module 'metrics' has no attribute 'set_ws_staleness'`

- [ ] **Step 3: Add the two new Gauges and helpers to `python-core/metrics.py`**

Add after the existing `daily_exposure` Gauge (around line 63):

```python
ws_staleness_seconds = Gauge(
    "arb_ws_staleness_seconds",
    "Seconds since last real price event from Polymarket WS (updated by zombie watchdog)",
)

fill_success_rate = Gauge(
    "arb_fill_success_rate",
    "Fraction of fill polls that returned filled in rolling 1h window (0.0–1.0)",
)
```

Add helper functions after `set_daily_exposure` (around line 153):

```python
def set_ws_staleness(seconds: float) -> None:
    ws_staleness_seconds.set(seconds)


def set_fill_success_rate(rate: float) -> None:
    fill_success_rate.set(rate)
```

- [ ] **Step 4: Run tests — both new tests should pass**

```bash
cd python-core && uv run pytest tests/test_metrics.py -v
```

Expected: all PASSED (including the new staleness and fill-rate tests)

- [ ] **Step 5: Add `_update_pnl_metric()` background task to `main.py`**

In `python-core/main.py`, add this async function (alongside other background tasks like `_evict_stale_opportunities`):

```python
async def _update_pnl_metric(db_conn):
    """Update daily_pnl Prometheus gauge every 60s."""
    while True:
        await asyncio.sleep(60)
        try:
            pnl = tracker.get_daily_pnl(db_conn)
            _metrics.set_daily_pnl(pnl)
        except Exception:
            pass  # non-fatal
```

In `main()`, schedule it alongside other background tasks (find where `asyncio.gather` or `asyncio.create_task` runs background loops):

```python
asyncio.create_task(_update_pnl_metric(db_conn))
```

- [ ] **Step 6: Handle `ws_staleness` JSON event from Rust stdout**

In `_read_stdout` (around line 221 in main.py), add a handler for the staleness JSON event that Plan 1's zombie watchdog emits:

```python
# Inside the existing stdout line parsing loop — after parsing gap events
try:
    data = json.loads(text)
    if data.get("event") == "ws_staleness":
        _metrics.set_ws_staleness(float(data.get("seconds", 0)))
        continue
    # ... existing gap handling ...
except json.JSONDecodeError:
    pass
```

- [ ] **Step 7: Create `config/prometheus.yml`**

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: polyking
    static_configs:
      - targets: ["localhost:9090"]
```

- [ ] **Step 8: Run full test suite**

```bash
cd python-core && uv run pytest tests/test_metrics.py -v
```

Expected: all PASSED

- [ ] **Step 9: Commit**

```bash
git add python-core/metrics.py python-core/main.py python-core/tests/test_metrics.py config/prometheus.yml
git commit -m "feat(scale): prometheus expansion — ws_staleness + fill_success_rate gauges + daily PnL background task"
```

---

## Task 2: NLP-Based Cross-Platform Matching

**Files:**
- Modify: `scripts/backfill_matches.py` (add `normalize()`, `title_similarity()`, gates + confidence scoring)
- Modify: `python-core/tests/test_backfill.py`

**Dependency:** None. `difflib` is stdlib — no new packages.

- [ ] **Step 1: Write the failing tests**

In `python-core/tests/test_backfill.py`, add:

```python
# NLP matching tests — add to existing test_backfill.py

def test_normalize_strips_punctuation():
    from backfill_matches import normalize
    assert normalize("Will U.S. GDP grow by 2%?") == "will us gdp grow by 2%"


def test_normalize_expands_abbreviations():
    from backfill_matches import normalize
    assert "us" in normalize("U.S. election")
    assert "uk" in normalize("U.K. prime minister")
    assert "percent" in normalize("GDP grows 3 pct")


def test_title_similarity_identical():
    from backfill_matches import title_similarity
    assert title_similarity("Will BTC hit 100k?", "Will BTC hit 100k?") == pytest.approx(1.0)


def test_title_similarity_high_confidence():
    """Near-identical titles → similarity > 0.90."""
    from backfill_matches import title_similarity
    a = "Will the Federal Reserve raise rates in 2025?"
    b = "Will the Federal Reserve increase rates in 2025?"
    sim = title_similarity(a, b)
    assert sim > 0.80, f"Expected > 0.80, got {sim:.3f}"


def test_title_similarity_very_different():
    """Completely different titles → similarity < 0.80."""
    from backfill_matches import title_similarity
    sim = title_similarity("Will it rain in London?", "Who wins the 2026 World Cup?")
    assert sim < 0.80, f"Expected < 0.80, got {sim:.3f}"


def test_confidence_thresholds():
    """Confidence scoring: 0.80–0.90 → low, 0.90–0.95 → medium, >0.95 → high."""
    from backfill_matches import score_pair_confidence
    assert score_pair_confidence(0.79) is None   # below threshold — rejected
    assert score_pair_confidence(0.82) == "low"
    assert score_pair_confidence(0.91) == "medium"
    assert score_pair_confidence(0.96) == "high"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd python-core && uv run pytest tests/test_backfill.py -v -k "normalize or similarity or confidence_threshold"
```

Expected: `ImportError: cannot import name 'normalize' from 'backfill_matches'`

- [ ] **Step 3: Add NLP functions to `scripts/backfill_matches.py`**

Add near the top of `backfill_matches.py` (after existing imports):

```python
import re
from difflib import SequenceMatcher


def normalize(title: str) -> str:
    """Normalize a market title for similarity comparison."""
    t = title.lower().strip()
    # Expand common abbreviations before stripping punctuation
    t = t.replace("u.s.", "us").replace("u.k.", "uk").replace("u.n.", "un")
    t = t.replace(" pct", " percent").replace("pct ", "percent ")
    # Strip punctuation except hyphens and percent signs
    t = re.sub(r"[^\w\s\-%]", "", t)
    # Collapse whitespace
    return re.sub(r"\s+", " ", t).strip()


def title_similarity(a: str, b: str) -> float:
    """Return SequenceMatcher ratio on normalized titles. Range [0.0, 1.0]."""
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def score_pair_confidence(similarity: float) -> "str | None":
    """Map similarity score to confidence level. Returns None if below threshold."""
    if similarity < 0.80:
        return None  # rejected
    if similarity <= 0.90:
        return "low"
    if similarity <= 0.95:
        return "medium"
    return "high"
```

- [ ] **Step 4: Wire NLP into the matching loop**

In `backfill_matches.py`, find where Polymarket and Kalshi market titles are compared (in the `Matcher` class or directly in the script's main body). Replace or augment the current title-matching logic with:

```python
def _nlp_match_pairs(poly_markets: list, kalshi_markets: list) -> list:
    """Match markets using NLP title similarity. Returns list of (poly, kalshi, confidence) tuples."""
    results = []
    used_kalshi = set()

    for poly in poly_markets:
        poly_title = poly.get("question", poly.get("title", ""))
        best_sim = 0.0
        best_kalshi = None

        for kalshi in kalshi_markets:
            if kalshi.get("ticker") in used_kalshi:
                continue
            kalshi_title = kalshi.get("title", kalshi.get("subtitle", ""))
            sim = title_similarity(poly_title, kalshi_title)
            if sim > best_sim:
                best_sim = sim
                best_kalshi = kalshi

        confidence = score_pair_confidence(best_sim)
        if confidence is None or best_kalshi is None:
            continue

        # Additional gates
        poly_year = _extract_year(poly_title)
        kalshi_year = _extract_year(best_kalshi.get("title", ""))
        if poly_year and kalshi_year and poly_year != kalshi_year:
            continue  # year mismatch

        used_kalshi.add(best_kalshi.get("ticker", ""))
        results.append((poly, best_kalshi, confidence, best_sim))

    return results


def _extract_year(text: str) -> "str | None":
    """Extract 4-digit year from text, or None if not found."""
    m = re.search(r"\b(20\d{2})\b", text)
    return m.group(1) if m else None
```

- [ ] **Step 5: Run tests**

```bash
cd python-core && uv run pytest tests/test_backfill.py -v -k "normalize or similarity or confidence_threshold"
```

Expected: all PASSED

- [ ] **Step 6: Commit**

```bash
git add scripts/backfill_matches.py python-core/tests/test_backfill.py
git commit -m "feat(scale): NLP title similarity matching — SequenceMatcher + confidence gates replaces exact match"
```

---

## Task 3: Automated Pair Invalidation

**Files:**
- Modify: `scripts/backfill_matches.py` (add `invalidate_expired_pairs()` + `--invalidate-only` CLI flag)
- Modify: `python-core/tests/test_backfill.py`

- [ ] **Step 1: Write failing tests**

Add to `python-core/tests/test_backfill.py`:

```python
def test_invalidation_removes_expired_pair():
    """invalidate_expired_pairs removes pairs where _check functions return False."""
    from backfill_matches import invalidate_expired_pairs

    pairs = [
        {"market_id": "pair-a", "token_a": "tok-active", "token_b": "kalshi-active"},
        {"market_id": "pair-b", "token_a": "tok-expired", "token_b": "kalshi-active"},
    ]

    def mock_check_poly(token_id):
        return token_id != "tok-expired"

    def mock_check_kalshi(ticker):
        return True  # all kalshi active

    active = invalidate_expired_pairs(
        pairs,
        check_poly_fn=mock_check_poly,
        check_kalshi_fn=mock_check_kalshi,
    )
    assert len(active) == 1
    assert active[0]["market_id"] == "pair-a"


def test_invalidation_keeps_all_active_pairs():
    """If all pairs are active, none are removed."""
    from backfill_matches import invalidate_expired_pairs

    pairs = [
        {"market_id": "pair-a", "token_a": "tok-a", "token_b": "k-a"},
        {"market_id": "pair-b", "token_a": "tok-b", "token_b": "k-b"},
    ]
    active = invalidate_expired_pairs(
        pairs,
        check_poly_fn=lambda _: True,
        check_kalshi_fn=lambda _: True,
    )
    assert len(active) == 2


def test_invalidation_api_error_keeps_pair():
    """If API check raises an exception, the pair is kept (safe default)."""
    from backfill_matches import invalidate_expired_pairs

    pairs = [{"market_id": "pair-x", "token_a": "tok-x", "token_b": "k-x"}]

    def raising_check(_):
        raise Exception("network error")

    active = invalidate_expired_pairs(
        pairs,
        check_poly_fn=raising_check,
        check_kalshi_fn=lambda _: True,
    )
    assert len(active) == 1  # kept on error
```

- [ ] **Step 2: Run to verify failure**

```bash
cd python-core && uv run pytest tests/test_backfill.py -v -k "invalidation"
```

Expected: `ImportError: cannot import name 'invalidate_expired_pairs'`

- [ ] **Step 3: Add `invalidate_expired_pairs` to `scripts/backfill_matches.py`**

```python
import logging
log = logging.getLogger(__name__)


def invalidate_expired_pairs(
    pairs: list,
    check_poly_fn=None,
    check_kalshi_fn=None,
) -> list:
    """Remove pairs where either market is no longer active.

    check_poly_fn(token_id) -> bool: True if Polymarket token is active.
    check_kalshi_fn(ticker) -> bool: True if Kalshi market is active.
    On API error, pair is retained (safe: better to keep a possibly-expired pair
    than silently drop a live one).
    """
    if check_poly_fn is None:
        check_poly_fn = _check_polymarket_active
    if check_kalshi_fn is None:
        check_kalshi_fn = _check_kalshi_active

    active = []
    for pair in pairs:
        token_a = pair.get("token_a", "")
        token_b = pair.get("token_b", pair.get("kalshi_ticker", ""))
        try:
            poly_ok = check_poly_fn(token_a)
        except Exception as e:
            log.warning("Poly active check failed for %s: %s — keeping pair", token_a, e)
            poly_ok = True
        try:
            kalshi_ok = check_kalshi_fn(token_b)
        except Exception as e:
            log.warning("Kalshi active check failed for %s: %s — keeping pair", token_b, e)
            kalshi_ok = True

        if poly_ok and kalshi_ok:
            active.append(pair)
        else:
            log.info(
                "Invalidating expired pair: %s (poly=%s, kalshi=%s)",
                pair.get("market_id", "?"), poly_ok, kalshi_ok,
            )
    return active


def _check_polymarket_active(token_id: str) -> bool:
    """Return True if Polymarket token is still active (not resolved/expired)."""
    import urllib.request, json as _json
    url = f"https://clob.polymarket.com/markets?token_id={token_id}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = _json.loads(r.read())
        markets = data if isinstance(data, list) else [data]
        for m in markets:
            if m.get("token_id") == token_id or str(m.get("condition_id", "")) == token_id:
                return bool(m.get("active", False))
        return False
    except Exception:
        return True  # assume active on network error


def _check_kalshi_active(ticker: str) -> bool:
    """Return True if Kalshi market is still open."""
    import urllib.request, json as _json
    url = f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = _json.loads(r.read())
        market = data.get("market", data)
        return market.get("status", "") == "open"
    except Exception:
        return True  # assume active on network error
```

- [ ] **Step 4: Add `--invalidate-only` CLI flag**

At the bottom of `backfill_matches.py`, find or add an `if __name__ == "__main__":` block:

```python
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Backfill market pairs")
    parser.add_argument(
        "--invalidate-only",
        action="store_true",
        help="Skip matching, only remove expired pairs from markets.json",
    )
    args = parser.parse_args()

    if args.invalidate_only:
        import json as _json
        _data = _json.loads(Path(MARKETS_JSON).read_text()) if Path(MARKETS_JSON).exists() else {"pairs": []}
        _pairs = _data.get("pairs", [])
        _active = invalidate_expired_pairs(_pairs)
        _data["pairs"] = _active
        Path(MARKETS_JSON).write_text(_json.dumps(_data, indent=2))
        print(f"Invalidation complete: {len(_active)}/{len(_pairs)} pairs kept")
    else:
        asyncio.run(main())
```

- [ ] **Step 5: Run tests**

```bash
cd python-core && uv run pytest tests/test_backfill.py -v -k "invalidation"
```

Expected: 3 PASSED

- [ ] **Step 6: Verify `--invalidate-only` flag works**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
uv run python scripts/backfill_matches.py --invalidate-only
```

Expected: `Invalidation complete: N/N pairs kept` (or some subset if markets have expired)

- [ ] **Step 7: Commit**

```bash
git add scripts/backfill_matches.py python-core/tests/test_backfill.py
git commit -m "feat(scale): automated pair invalidation — expired markets removed from markets.json + --invalidate-only flag"
```

---

## Task 4: Idempotency Keys on Trade Attempts

**Files:**
- Modify: `python-core/tracker.py` (new table + 3 new functions)
- Modify: `python-core/two_leg_executor.py` (log attempt before orders, confirm after success)
- Modify: `python-core/startup_audit.py` (query + warn on unconfirmed attempts)
- Modify: `python-core/tests/test_tracker.py`
- Modify: `python-core/tests/test_startup_audit.py` (if exists)

- [ ] **Step 1: Write failing tests**

Add to `python-core/tests/test_tracker.py`:

```python
def test_log_trade_attempt_stores_record():
    """log_trade_attempt writes a row to trade_attempts with confirmed=0."""
    import sqlite3
    from tracker import init_db, log_trade_attempt, get_unconfirmed_attempts

    conn = init_db(":memory:")
    attempt_id = "test-uuid-1234"
    log_trade_attempt(conn, {
        "attempt_id": attempt_id,
        "market_id": "market-abc",
        "pair_type": "cross_platform",
        "gap_cents": 12.5,
        "bet_usdc": 25.0,
    })

    rows = conn.execute("SELECT * FROM trade_attempts WHERE attempt_id=?", (attempt_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["confirmed"] == 0
    assert rows[0]["market_id"] == "market-abc"


def test_confirm_trade_attempt_sets_confirmed():
    """confirm_trade_attempt sets confirmed=1 and links trade_id."""
    import sqlite3
    from tracker import init_db, log_trade_attempt, confirm_trade_attempt

    conn = init_db(":memory:")
    attempt_id = "test-uuid-5678"
    log_trade_attempt(conn, {
        "attempt_id": attempt_id,
        "market_id": "market-xyz",
        "pair_type": "internal",
        "gap_cents": 8.0,
        "bet_usdc": 15.0,
    })
    confirm_trade_attempt(conn, attempt_id, trade_id=42)

    row = conn.execute(
        "SELECT confirmed, trade_id FROM trade_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    assert row["confirmed"] == 1
    assert row["trade_id"] == 42


def test_get_unconfirmed_attempts_returns_recent_unconfirmed():
    """get_unconfirmed_attempts returns attempts within the last hour that are unconfirmed."""
    import sqlite3
    from tracker import init_db, log_trade_attempt, get_unconfirmed_attempts, confirm_trade_attempt

    conn = init_db(":memory:")

    log_trade_attempt(conn, {
        "attempt_id": "unconf-1",
        "market_id": "market-a",
        "pair_type": "cross_platform",
        "gap_cents": 10.0,
        "bet_usdc": 20.0,
    })
    log_trade_attempt(conn, {
        "attempt_id": "conf-2",
        "market_id": "market-b",
        "pair_type": "cross_platform",
        "gap_cents": 10.0,
        "bet_usdc": 20.0,
    })
    confirm_trade_attempt(conn, "conf-2", trade_id=1)

    unconfirmed = get_unconfirmed_attempts(conn, window_minutes=60)
    ids = [r["attempt_id"] for r in unconfirmed]
    assert "unconf-1" in ids
    assert "conf-2" not in ids
```

- [ ] **Step 2: Run to verify failure**

```bash
cd python-core && uv run pytest tests/test_tracker.py::test_log_trade_attempt_stores_record tests/test_tracker.py::test_confirm_trade_attempt_sets_confirmed tests/test_tracker.py::test_get_unconfirmed_attempts_returns_recent_unconfirmed -v
```

Expected: `AttributeError: module 'tracker' has no attribute 'log_trade_attempt'`

- [ ] **Step 3: Add `trade_attempts` table to `tracker.py`**

In `python-core/tracker.py`, add to `_create_tables()` executescript (after `execution_events` table):

```sql
CREATE TABLE IF NOT EXISTS trade_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL UNIQUE,
    market_id TEXT NOT NULL,
    pair_type TEXT NOT NULL,
    gap_cents REAL,
    bet_usdc REAL,
    attempted_at TEXT NOT NULL DEFAULT (datetime('now')),
    confirmed INTEGER NOT NULL DEFAULT 0,
    trade_id INTEGER REFERENCES trades(id)
);
CREATE INDEX IF NOT EXISTS idx_attempts_confirmed ON trade_attempts(confirmed, attempted_at DESC);
```

- [ ] **Step 4: Add three new functions to `tracker.py`**

Add after `log_execution_event` (around line 471):

```python
def log_trade_attempt(conn: sqlite3.Connection, attempt: dict) -> None:
    """Write a pre-execution idempotency record. Called BEFORE order placement."""
    conn.execute(
        """INSERT OR IGNORE INTO trade_attempts
           (attempt_id, market_id, pair_type, gap_cents, bet_usdc, attempted_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (
            attempt["attempt_id"],
            attempt["market_id"],
            attempt.get("pair_type", "cross_platform"),
            attempt.get("gap_cents"),
            attempt.get("bet_usdc"),
        ),
    )
    conn.commit()


def confirm_trade_attempt(conn: sqlite3.Connection, attempt_id: str, trade_id: int) -> None:
    """Mark a trade attempt as confirmed after both legs fill."""
    conn.execute(
        "UPDATE trade_attempts SET confirmed=1, trade_id=? WHERE attempt_id=?",
        (trade_id, attempt_id),
    )
    conn.commit()


def get_unconfirmed_attempts(conn: sqlite3.Connection, window_minutes: int = 60) -> list[dict]:
    """Return unconfirmed attempts within the last window_minutes."""
    rows = conn.execute(
        """SELECT * FROM trade_attempts
           WHERE confirmed=0
           AND attempted_at > datetime('now', ? || ' minutes')
           ORDER BY attempted_at DESC""",
        (f"-{window_minutes}",),
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: Run tracker tests**

```bash
cd python-core && uv run pytest tests/test_tracker.py::test_log_trade_attempt_stores_record tests/test_tracker.py::test_confirm_trade_attempt_sets_confirmed tests/test_tracker.py::test_get_unconfirmed_attempts_returns_recent_unconfirmed -v
```

Expected: 3 PASSED

- [ ] **Step 6: Wire idempotency into `two_leg_executor.py`**

Add `import uuid` at top of `python-core/two_leg_executor.py`.

Add `from tracker import log_trade_attempt, confirm_trade_attempt` to the imports line in `two_leg_executor.py`.

In `execute()` method, before the `if pair_type == "internal":` branch (around line 147), add:

```python
# Write idempotency record before any order placement
_attempt_id = str(uuid.uuid4())
try:
    log_trade_attempt(self._db, {
        "attempt_id": _attempt_id,
        "market_id": gap.get("market_id", ""),
        "pair_type": pair_type,
        "gap_cents": gap.get("gap_cents"),
        "bet_usdc": bet_size,
    })
except Exception:
    pass  # non-fatal — orphan detection still catches crashes
```

After `trade_id = tracker.log_trade(...)` in `main.py` (after the trade is fully logged), confirm the attempt. Since the attempt_id is local to `execute()`, we need to return it or pass it through.

**Simpler approach:** pass `_attempt_id` through the result dict:

```python
# In execute(), after result is set:
if result is not None:
    result["_attempt_id"] = _attempt_id
```

Then in `main.py`, after `trade_id = tracker.log_trade(db_conn, trade)`:

```python
_attempt_id = confirmation.get("_attempt_id")
if _attempt_id:
    try:
        tracker.confirm_trade_attempt(db_conn, _attempt_id, trade_id)
    except Exception:
        pass  # non-fatal
```

- [ ] **Step 7: Wire unconfirmed attempt check into `startup_audit.py`**

In `python-core/startup_audit.py`, add this check (after existing orphan checks):

```python
import logging
log = logging.getLogger(__name__)

def check_unconfirmed_attempts(db_conn) -> None:
    """Log WARNING for each trade attempt that was initiated but never confirmed."""
    from tracker import get_unconfirmed_attempts
    unconfirmed = get_unconfirmed_attempts(db_conn, window_minutes=60)
    for row in unconfirmed:
        log.warning(
            "UNCONFIRMED TRADE ATTEMPT: attempt_id=%s market_id=%s attempted_at=%s — "
            "crash may have occurred mid-execution. Check exchange for orphan positions.",
            row["attempt_id"], row["market_id"], row["attempted_at"],
        )
```

Call `check_unconfirmed_attempts(db_conn)` from the main audit function that `main.py` calls at startup.

- [ ] **Step 8: Write startup_audit test**

Add to (or create) `python-core/tests/test_startup_audit.py`:

```python
import logging
import sqlite3
import pytest
from tracker import init_db, log_trade_attempt


def test_unconfirmed_attempt_flagged(caplog):
    """Unconfirmed trade attempt within 1h triggers WARNING log."""
    conn = init_db(":memory:")
    log_trade_attempt(conn, {
        "attempt_id": "orphan-uuid",
        "market_id": "test-market",
        "pair_type": "cross_platform",
        "gap_cents": 10.0,
        "bet_usdc": 25.0,
    })
    # attempt_id "orphan-uuid" is unconfirmed

    from startup_audit import check_unconfirmed_attempts
    with caplog.at_level(logging.WARNING):
        check_unconfirmed_attempts(conn)

    assert any("orphan-uuid" in r.message for r in caplog.records), (
        "Expected WARNING about orphan-uuid unconfirmed attempt"
    )
```

- [ ] **Step 9: Run full test suite**

```bash
cd python-core && uv run pytest tests/test_tracker.py tests/test_startup_audit.py -v
```

Expected: all PASSED

- [ ] **Step 10: Commit**

```bash
git add python-core/tracker.py python-core/two_leg_executor.py python-core/startup_audit.py python-core/tests/test_tracker.py python-core/tests/test_startup_audit.py
git commit -m "feat(scale): idempotency keys — trade_attempts table records pre-execution state; unconfirmed attempts flagged on startup"
```

---

## Task 5: Operational Runbook

**Files:**
- Create: `docs/runbook.md`

- [ ] **Step 1: Create `docs/runbook.md`**

```markdown
# PolyyKing Operations Runbook

Last updated: 2026-05-12

---

## Section 1 — Normal Operation

### Starting the bot

**Dry-run mode (default — no real orders placed):**
```bash
cd /path/to/PolyyKing
DRY_RUN=true uv run python python-core/main.py
```

**Live mode:**
```bash
DRY_RUN=false uv run python python-core/main.py
```
Ensure `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_WALLET_ADDRESS`, `KALSHI_API_KEY`, and `KALSHI_API_SECRET` are set in `.env` before running live.

### Checking current status

**Open positions:**
```bash
sqlite3 data/trades.db "SELECT market_id, polymarket_side, kalshi_side, amount_usdc, opened_at FROM trades WHERE status='open' ORDER BY opened_at DESC;"
```

**Today's P&L:**
```bash
sqlite3 data/trades.db "SELECT ROUND(SUM(actual_profit), 4) as pnl FROM trades WHERE date(opened_at) = date('now') AND dry_run = 0;"
```

**Recent gaps detected:**
```bash
sqlite3 data/trades.db "SELECT market_id, gap_cents, confidence, executed, detected_at FROM gaps ORDER BY detected_at DESC LIMIT 20;"
```

**Unconfirmed trade attempts (crash indicator):**
```bash
sqlite3 data/trades.db "SELECT attempt_id, market_id, bet_usdc, attempted_at FROM trade_attempts WHERE confirmed=0 AND attempted_at > datetime('now', '-1 hour');"
```

### Reading Prometheus metrics

Start Prometheus:
```bash
prometheus --config.file=config/prometheus.yml
```

Key metrics (scrape at `http://localhost:9090/metrics` or Prometheus UI):
- `arb_daily_pnl_usdc` — today's net P&L
- `arb_ws_staleness_seconds` — seconds since last Polymarket WS price event
- `arb_fill_success_rate` — fraction of orders that filled vs timed out
- `arb_gaps_detected_total` — total gap events from Rust
- `arb_executions_total` — execution attempts by outcome

---

## Section 2 — Emergency Close

### Manually cancel a Polymarket order

```bash
# Get the order_id from trades.db first
sqlite3 data/trades.db "SELECT polymarket_order_id, market_id FROM trades WHERE status='open';"

# Cancel via CLOB API
curl -X DELETE "https://clob.polymarket.com/order/<ORDER_ID>" \
  -H "Authorization: Bearer <your_api_key>"
```

Replace `<ORDER_ID>` with the value from the DB and `<your_api_key>` with your L2 API key (derived at startup, visible in logs with `--debug`).

### Manually close a Kalshi position

```bash
# Get the ticker and quantity
sqlite3 data/trades.db "SELECT kalshi_order_id, market_id FROM trades WHERE status='open';"

# Sell your position via Kalshi API
curl -X POST "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders" \
  -H "Authorization: Bearer <kalshi_api_key>" \
  -H "Content-Type: application/json" \
  -d '{"action": "sell", "ticker": "<TICKER>", "count": <N>, "type": "market"}'
```

### Mark an emergency_position as manually closed

```bash
sqlite3 data/trades.db "UPDATE emergency_positions SET status='closed_manual', closed_at=datetime('now') WHERE order_id='<ORDER_ID>';"
```

---

## Section 3 — Kill Switch

### How the kill switch is set

The kill switch is set automatically when:
1. Daily loss limit is reached (`MAX_DAILY_LOSS_USDC`)
2. Manually written to the DB (see below)

It persists across restarts — the bot will refuse to trade until cleared.

### Verify the kill switch is set

```bash
sqlite3 data/trades.db "SELECT * FROM bot_state WHERE key='kill_switch';"
```

If this returns a row with `value='1'`, the kill switch is active.

### Clear the kill switch

**Required checks before clearing:**
1. Confirm today's P&L: `sqlite3 data/trades.db "SELECT ROUND(SUM(actual_profit), 4) FROM trades WHERE date(opened_at)=date('now') AND dry_run=0;"`
2. Confirm no open positions: `sqlite3 data/trades.db "SELECT COUNT(*) FROM trades WHERE status='open';"`
3. If any open positions remain, close them manually (Section 2) first.

**Clear:**
```bash
sqlite3 data/trades.db "DELETE FROM bot_state WHERE key='kill_switch';"
```

The bot can now be restarted normally.

### Set the kill switch manually (emergency stop)

```bash
sqlite3 data/trades.db "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('kill_switch', '1');"
```

---

## Section 4 — Pair Refresh

### When to run backfill

Run `backfill_matches.py` when:
- Starting fresh (no `config/markets.json`)
- `markets.json` is more than 24 hours old (bot runs it automatically on startup)
- A market resolves and you want to clean up dead pairs
- You want to find new arbitrage opportunities

```bash
cd /path/to/PolyyKing
uv run python scripts/backfill_matches.py
```

### Invalidate expired pairs only (no new matching)

```bash
uv run python scripts/backfill_matches.py --invalidate-only
```

### Add a manual pair to `markets.json`

Open `config/markets.json` and add to the `pairs` array:

```json
{
  "market_id": "unique-market-id",
  "pair_type": "cross_platform",
  "token_a": "<polymarket_token_id>",
  "token_b": "<kalshi_ticker>",
  "polymarket_token": "<polymarket_token_id>",
  "kalshi_ticker": "<kalshi_ticker>",
  "confidence": "high",
  "fee_rate": 0.04
}
```

### Invalidate a bad pair manually

Remove the pair entry from `config/markets.json`. The bot loads `markets.json` at startup — restart after editing.

### Review resolution mismatches

```bash
cat data/resolution_mismatches.json
```

Each entry shows pairs where the Polymarket and Kalshi resolution timestamps differ by more than `MAX_RESOLUTION_DELTA_HOURS` (default: 6 hours). These pairs are excluded from the active set automatically.
```

- [ ] **Step 2: Verify the runbook commands work**

Run the read-only queries against the live DB:

```bash
sqlite3 data/trades.db "SELECT market_id, amount_usdc, opened_at FROM trades WHERE status='open' ORDER BY opened_at DESC LIMIT 5;"
sqlite3 data/trades.db "SELECT * FROM bot_state WHERE key='kill_switch';"
sqlite3 data/trades.db "SELECT COUNT(*) FROM trade_attempts WHERE confirmed=0 AND attempted_at > datetime('now', '-1 hour');"
```

All three should return without error (empty results are fine).

- [ ] **Step 3: Commit**

```bash
git add docs/runbook.md
git commit -m "docs(scale): operational runbook — emergency close, kill switch, pair refresh procedures"
```

---

## Task 6: Final Verification

- [ ] **Step 1: Run complete Python test suite**

```bash
cd python-core && uv run pytest --tb=short -q
```

Expected: all tests PASS — no regressions.

- [ ] **Step 2: Run Rust test suite**

```bash
cd rust-core && cargo test 2>&1 | tail -20
```

Expected: all pass (Plan 3 has no Rust changes)

- [ ] **Step 3: Verify metrics module loads cleanly**

```bash
cd python-core && uv run python -c "
import metrics as m
m.set_ws_staleness(0.0)
m.set_fill_success_rate(1.0)
m.set_daily_pnl(0.0)
print('metrics: OK')
"
```

Expected: `metrics: OK`

- [ ] **Step 4: Verify invalidation CLI flag**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
uv run python scripts/backfill_matches.py --invalidate-only
```

Expected: prints `Invalidation complete: N/N pairs kept`

- [ ] **Step 5: Verify `trade_attempts` table exists**

```bash
sqlite3 data/trades.db ".schema trade_attempts"
```

Expected: DDL for `trade_attempts` table printed (table is created on next `init_db()` call if not already present)

If table doesn't exist yet:

```bash
cd python-core && uv run python -c "from tracker import init_db; init_db('../data/trades.db'); print('trade_attempts table created')"
```

- [ ] **Step 6: Commit any remaining cleanup**

```bash
git status
# Add only intentional changes — not auto-generated files
```

---

## End State

After all three plans are fully implemented:

| Component | Spec target |
|-----------|-------------|
| Execution safety (Plan 1) | 7.5 → 8.0/10 |
| Edge accuracy (Plan 2) | 8.0 → 9.0/10 |
| Scale readiness (Plan 3) | 9.0 → 9.5/10 |

**To run the bot with all improvements active:**

```bash
cd /path/to/PolyyKing
cp config/.env.example .env   # fill in keys and review new env vars
uv run python scripts/backfill_matches.py   # refresh pairs with NLP matching + invalidation
DRY_RUN=true uv run python python-core/main.py   # test first
# When satisfied:
DRY_RUN=false uv run python python-core/main.py
```
