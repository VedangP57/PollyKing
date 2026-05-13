# Production Hardening — Design Spec

**Date:** 2026-05-13  
**Status:** Approved  
**Scope:** Four targeted changes to close the remaining production-readiness gaps identified in the audit.

---

## Problem

The audit scored the bot 9.5/10 with four concrete gaps before risking live capital:

| # | Gap | Risk |
|---|-----|------|
| 1 | No HTTP 429 retry in either executor | Rate-limit burst leaves one leg open, one-sided position |
| 2 | No Kalshi idempotent order key | POST timeout → unknown if order landed → potential double-fill |
| 3 | `py_clob_client_v2` is an unofficial fork | Unknown production track record for auth/signing |
| 4 | No paper-trade observation pipeline | No way to measure whether EV estimates hold at settlement |

---

## Architecture

Four isolated changes. No cross-wiring with `main.py`, `detector.py`, or `two_leg_executor.py`.

```
python-core/
  http_utils.py               NEW  — shared async retry helper
  kalshi_executor.py          MOD  — use http_utils, add client_order_id
  polymarket_executor.py      MOD  — swap import, add sync retry loop
  requirements.txt            MOD  — py_clob_client_v2 → py-clob-client
  tests/
    test_kalshi_executor.py   MOD  — add 429 retry test, idempotent key tests
    test_polymarket_executor.py MOD — add 429 retry test

scripts/
  dry_run_audit.py            NEW  — reads trades.db, reports EV accuracy
```

---

## Component 1: `http_utils.py`

Single public function:

```python
async def async_retry_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    **kwargs,
) -> aiohttp.ClientResponse:
```

**Behaviour:**
- On HTTP 429: reads `Retry-After` header; sleeps that many seconds (min 1s). If header absent, uses exponential backoff: `1s → 2s → 4s`.
- On any other non-2xx: raises immediately — no retry. The circuit breaker upstream handles persistent failures.
- Returns the successful `ClientResponse` — callers do `await resp.json()` exactly as today.
- Raises `ExecutorError` if all retries exhausted.
- Does not catch network-layer errors (`aiohttp.ClientError`); those propagate to the circuit breaker.

---

## Component 2: `KalshiExecutor` changes

### 2a — Retry

All `session.post / session.get / session.delete` calls replaced with `async_retry_request(session, method, url, ...)`. Signature at call sites is unchanged; only the function being called differs.

### 2b — Idempotent order key

`place_order` generates `client_order_id = str(uuid.uuid4())` before the POST and includes it in the request body:

```python
body = {
    "ticker": ticker,
    "action": action,
    "count": int(count),
    "type": "market",
    "client_order_id": client_order_id,
}
```

On HTTP 409 Conflict (duplicate key): attempt `GET /portfolio/orders?client_order_id=<id>`. If Kalshi does not support that query param, fall back to `GET /portfolio/orders` and scan the list for a matching `client_order_id`. Return the found order. This makes `place_order` safe to call twice on network timeout — the second call returns the same order rather than creating a second one.

---

## Component 3: `PolymarketExecutor` changes

### 3a — Dependency swap

`requirements.txt`: remove `py_clob_client_v2`, add `py-clob-client` (official Polymarket package).

`polymarket_executor.py` imports updated:
- `from py_clob_client_v2 import ...` → `from py_clob_client import ...`
- `PartialCreateOrderOptions` may be `CreateOrderOptions` in the official client — resolve at implementation time by inspecting the installed package.

All other executor logic (two-phase auth, FOK→GTC fallback, fee cache) is unchanged.

### 3b — Sync retry loop

`_place_sync` wraps `create_and_post_order` in a retry loop:

```python
for attempt in range(3):
    try:
        resp = client.create_and_post_order(...)
        break
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        raise
```

This runs in the thread executor so `time.sleep` is safe (it does not block the asyncio loop).

---

## Component 4: `scripts/dry_run_audit.py`

**CLI usage:**
```bash
python scripts/dry_run_audit.py [--db path/to/trades.db]
```

**What it does:**
1. Resolves `DB_PATH` from env, or uses `--db` arg, defaulting to `data/trades.db`.
2. Opens `trades.db` read-only.
3. Queries: total dry-run trades, resolved count, open count.
4. Calls `calibration.brier_score(conn)`, `calibration.win_rate(conn)`, `calibration.ev_error(conn)`.
5. Queries `trade_attempts` for unconfirmed rows older than 60 minutes.
6. Prints a plain-text summary — no external deps beyond what the project already has.

**Output format:**
```
Dry-run audit — 127 trades (93 resolved, 34 open)
Win rate:      61.3%
Mean EV error: +0.8c  (predicted higher than actual)
Brier score:   0.21   (lower is better; 0.25 = random)
Unconfirmed trade_attempts: 0
```

Exits with code 1 if Brier score > 0.30 (worse than random) — can be used as a go/no-go gate before switching to live.

---

## Testing

| New test | What it covers |
|---|---|
| `test_kalshi_executor.py::test_429_retries_then_succeeds` | Mock 429 × 2 then 201; assert `place_order` returns normally and retried 3 times total |
| `test_kalshi_executor.py::test_idempotent_key_on_409` | Mock 409 then GET returning existing order; assert same `order_id` returned |
| `test_polymarket_executor.py::test_place_sync_retries_on_429` | Mock `requests.HTTPError` with status 429 × 1 then success; assert order returned |
| `test_dry_run_audit.py` | In-memory SQLite with known trades; assert correct win_rate and Brier score in output |

All existing 224 tests must remain green. No test structure changes.

---

## Out of Scope

- Changes to `main.py`, `detector.py`, `two_leg_executor.py`, `circuit_breaker.py`
- Reconciler or tracker changes
- Prometheus metric additions
- Rust core changes
