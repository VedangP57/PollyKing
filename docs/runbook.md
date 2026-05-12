# PolyyKing Operational Runbook

---

## Section 1 — Normal operation

### Starting the bot

**Dry run (safe — no real orders placed):**
```bash
DRY_RUN=true uv run python python-core/main.py
```

**Live trading:**
```bash
DRY_RUN=false uv run python python-core/main.py
```

Startup will validate API keys, exchange connectivity, and DB integrity before the Rust process spawns. Any failure exits with a one-line error.

### Checking current status

```bash
# Open positions
sqlite3 data/trades.db "SELECT market_id, amount_usdc, opened_at FROM trades WHERE status='open' AND dry_run=0;"

# Today's P&L
sqlite3 data/trades.db "SELECT SUM(actual_profit) FROM trades WHERE DATE(closed_at)=DATE('now') AND dry_run=0;"

# Recent gaps detected
sqlite3 data/trades.db "SELECT market_id, gap_cents, detected_at FROM gaps ORDER BY detected_at DESC LIMIT 20;"

# Unconfirmed trade attempts (crash indicator)
sqlite3 data/trades.db "SELECT attempt_id, market_id, attempted_at FROM trade_attempts WHERE confirmed=0 AND attempted_at > datetime('now', '-1 hour');"
```

### Reading Prometheus metrics

Scrape endpoint: `http://localhost:9090/metrics`

Run local Prometheus:
```bash
prometheus --config.file=config/prometheus.yml
```

Key metrics:
| Metric | Meaning |
|--------|---------|
| `arb_daily_pnl_usdc` | Net P&L today (updates every 60s) |
| `arb_ws_staleness_seconds` | Seconds since last Polymarket WS price event |
| `arb_fill_success_rate` | Fill poll success fraction (rolling 1h) |
| `arb_gaps_detected_total` | Total gaps seen since startup |
| `arb_executions_total{outcome="success"}` | Successful trades |

---

## Section 2 — Emergency close

### Cancel a Polymarket order manually

```bash
# Replace ORDER_ID and TOKEN_ID with actual values from trades.db
curl -X DELETE "https://clob.polymarket.com/order/ORDER_ID" \
  -H "Authorization: Bearer $POLYMARKET_API_KEY"
```

### Close a Kalshi position manually

```bash
# Sell back the position (replace TICKER, SIDE, COUNT)
curl -X POST "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders" \
  -H "Authorization: Token $KALSHI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"ticker": "TICKER", "action": "sell", "side": "SIDE", "count": COUNT, "type": "market"}'
```

### Mark an emergency position as closed in DB

```bash
sqlite3 data/trades.db \
  "UPDATE emergency_positions SET status='closed', closed_at=datetime('now') WHERE order_id='ORDER_ID';"
```

---

## Section 3 — Kill switch

### How the kill switch gets set

The kill switch is set automatically when:
- `max_daily_loss_usdc` is reached (`detector.validate()` raises `SystemExit(1)`)
- Manually via the command below

### Verify it is set

```bash
sqlite3 data/trades.db "SELECT * FROM bot_state WHERE key='kill_switch';"
```

A row with `value='1'` means the bot will refuse to execute trades on restart.

### Clear the kill switch (resume trading)

**Required checks before clearing:**
1. Confirm today's P&L: `sqlite3 data/trades.db "SELECT SUM(actual_profit) FROM trades WHERE DATE(opened_at)=DATE('now') AND dry_run=0;"`
2. Confirm no open positions: `sqlite3 data/trades.db "SELECT COUNT(*) FROM trades WHERE status='open' AND dry_run=0;"`
3. Confirm no unconfirmed attempts: `sqlite3 data/trades.db "SELECT COUNT(*) FROM trade_attempts WHERE confirmed=0 AND attempted_at > datetime('now', '-1 hour');"`

**Clear:**
```bash
sqlite3 data/trades.db "DELETE FROM bot_state WHERE key='kill_switch';"
```

---

## Section 4 — Pair refresh

### When to run backfill_matches.py

- After deploying a new version (pairs may have expired)
- When you see frequent "no markets matched" or ghost gaps in logs
- Weekly as routine maintenance

### Run full refresh

```bash
uv run python scripts/backfill_matches.py
```

### Invalidate expired pairs only (no re-matching)

```bash
uv run python scripts/backfill_matches.py --invalidate-only
```

### Add a manual pair

Edit `config/markets.json` and add an entry to the `"manual_pairs"` array:
```json
{
  "pair_type": "cross_platform",
  "token_a": "POLYMARKET_YES_TOKEN_HEX",
  "no_token_a": "POLYMARKET_NO_TOKEN_HEX",
  "token_b": "KALSHI_TICKER",
  "market_id": "manual-descriptive-id",
  "confidence": "high",
  "match_method": "manual"
}
```

### Invalidate a bad pair

Remove the entry from `"pairs"` in `config/markets.json`, or run `--invalidate-only` which will remove any pair where either exchange API reports the market as closed.

### Resolution mismatch review

```bash
cat data/resolution_mismatches.json
```

Pairs here were excluded because Kalshi and Polymarket close at different times (delta > `MAX_RESOLUTION_DELTA_HOURS`). Review manually before adding them back as manual pairs.
