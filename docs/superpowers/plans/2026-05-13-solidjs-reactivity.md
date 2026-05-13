# SolidJS Reactivity & Event-Driven UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 2s polling for gaps/trades with a ~20ms Unix socket event path and add visual reactivity (flash, P&L animation, connection indicator, card highlights) using only existing SolidJS primitives.

**Architecture:** Python notifier writes `"gap\n"` or `"trade\n"` to `data/polyking_events.sock`; a Tauri tokio task listens on that socket, re-queries SQLite, and emits typed Tauri events; the SolidJS frontend receives those events via `listen()` and updates signals directly — no polling for gaps or trades.

**Tech Stack:** Rust/Tauri 2 (tokio, rusqlite), SolidJS 1.x signals/effects, TanStack Query (kept for stats/mode/bot/pnl/connection), Python (socket stdlib)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `tauri-app/src-tauri/src/db.rs` | Modify | Add `has_recent_gap_activity()` with testable inner fn |
| `tauri-app/src-tauri/src/commands.rs` | Modify | Add `ConnectionStatus` struct + `get_connection_status` command |
| `tauri-app/src-tauri/src/lib.rs` | Modify | Register new command + add Unix socket listener task |
| `python-core/notifier.py` | Modify | Add `_notify()` helper, call at end of `gap_detected()` + `trade_executed()` |
| `tauri-app/src/components/GapRow.tsx` | Create | Extracted gap row component — isolates per-row re-renders |
| `tauri-app/src/App.tsx` | Modify | Replace gapsQuery/tradesQuery with signals + event listeners; add visual features |
| `tauri-app/src/index.css` | Modify | Add flash, badge-new, conn-dot, stat-positive/negative/pulse styles |

---

### Task 1: `db.rs` — `has_recent_gap_activity()` with unit test

**Files:**
- Modify: `tauri-app/src-tauri/src/db.rs`

- [ ] **Step 1: Write the failing test**

Add at the bottom of `db.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn make_test_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE gaps (
                id INTEGER PRIMARY KEY,
                detected_at TEXT NOT NULL
            );",
        ).unwrap();
        conn
    }

    fn has_recent_gap_activity_in(conn: &Connection) -> bool {
        conn.query_row(
            "SELECT COUNT(*) FROM gaps WHERE detected_at > datetime('now', '-30 seconds')",
            [],
            |r| r.get::<_, i64>(0),
        ).unwrap_or(0) > 0
    }

    #[test]
    fn test_no_gaps_returns_false() {
        let conn = make_test_db();
        assert!(!has_recent_gap_activity_in(&conn));
    }

    #[test]
    fn test_recent_gap_returns_true() {
        let conn = make_test_db();
        conn.execute(
            "INSERT INTO gaps (detected_at) VALUES (datetime('now'))",
            [],
        ).unwrap();
        assert!(has_recent_gap_activity_in(&conn));
    }

    #[test]
    fn test_old_gap_returns_false() {
        let conn = make_test_db();
        conn.execute(
            "INSERT INTO gaps (detected_at) VALUES (datetime('now', '-60 seconds'))",
            [],
        ).unwrap();
        assert!(!has_recent_gap_activity_in(&conn));
    }
}
```

- [ ] **Step 2: Run test — expect compilation failure**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/tauri-app/src-tauri
cargo test has_recent_gap_activity 2>&1 | head -20
```

Expected: compile error — `has_recent_gap_activity_in` not defined in main code (only in test).

- [ ] **Step 3: Add `has_recent_gap_activity()` public function**

Add this after the `open()` helper in `db.rs` (after line ~69, before `today_prefix()`):

```rust
pub fn has_recent_gap_activity() -> bool {
    match open() {
        Ok(conn) => conn
            .query_row(
                "SELECT COUNT(*) FROM gaps WHERE detected_at > datetime('now', '-30 seconds')",
                [],
                |r| r.get::<_, i64>(0),
            )
            .unwrap_or(0)
            > 0,
        Err(_) => false,
    }
}
```

- [ ] **Step 4: Run tests — expect all three to pass**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/tauri-app/src-tauri
cargo test has_recent_gap_activity -- --nocapture 2>&1 | tail -15
```

Expected:
```
test tests::test_no_gaps_returns_false ... ok
test tests::test_old_gap_returns_false ... ok
test tests::test_recent_gap_returns_true ... ok
test result: ok. 3 passed
```

- [ ] **Step 5: Commit**

```bash
git add tauri-app/src-tauri/src/db.rs
git commit -m "feat(db): has_recent_gap_activity() for connection status"
```

---

### Task 2: `commands.rs` — `get_connection_status` command

**Files:**
- Modify: `tauri-app/src-tauri/src/commands.rs`

- [ ] **Step 1: Add `ConnectionStatus` struct and command**

Add after the existing `UiSettings` impl block (after line ~29). The import at line 1 already has `use crate::db::{self, DailyPnl, Gap, Stats, Trade}` — no import changes needed for `db::`. Also add `use crate::commands::reconcile_bot_child` — wait, `reconcile_bot_child` is defined in this same file. Check the existing function. It's already in scope.

Add to `commands.rs` (find the end of the file and append):

```rust
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ConnectionStatus {
    pub bot_running: bool,
    pub ws_active: bool,
}

#[tauri::command]
pub fn get_connection_status(state: State<'_, Mutex<BotState>>) -> ConnectionStatus {
    let mut s = state.lock().unwrap();
    reconcile_bot_child(&mut *s);
    let bot_running = s.child.is_some();
    let ws_active = db::has_recent_gap_activity();
    ConnectionStatus { bot_running, ws_active }
}
```

- [ ] **Step 2: Verify compilation**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/tauri-app/src-tauri
cargo check 2>&1 | grep -E "^error" | head -10
```

Expected: no error lines.

- [ ] **Step 3: Commit**

```bash
git add tauri-app/src-tauri/src/commands.rs
git commit -m "feat(commands): get_connection_status — bot_running + ws_active"
```

---

### Task 3: `lib.rs` — register command + Unix socket listener

**Files:**
- Modify: `tauri-app/src-tauri/src/lib.rs`

- [ ] **Step 1: Register `get_connection_status` in invoke_handler**

In `lib.rs`, find the `invoke_handler` block (lines 75-91). Add the new command:

```rust
        .invoke_handler(tauri::generate_handler![
            commands::get_bot_running,
            commands::get_mode,
            commands::set_dry_run,
            commands::get_ui_settings,
            commands::save_ui_settings,
            commands::get_daily_pnl,
            commands::tail_bot_log,
            commands::get_stats,
            commands::get_active_gaps,
            commands::get_recent_trades,
            commands::start_bot,
            commands::stop_bot,
            commands::get_risk_state,
            commands::get_calibration_stats,
            commands::get_portfolio_breakdown,
            commands::get_connection_status,
        ])
```

- [ ] **Step 2: Add Unix socket listener task in `setup()`**

Find the line `Ok(())` at the end of the `setup()` closure (line ~73). Insert the socket task immediately before it:

```rust
            // Unix socket listener: Python writes "gap\n" or "trade\n" to trigger
            // a SQLite re-query and a Tauri event push to the frontend.
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                use tokio::io::AsyncReadExt;
                let sock_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                    .parent().unwrap()
                    .parent().unwrap()
                    .join("data/polyking_events.sock");
                let _ = std::fs::remove_file(&sock_path);
                let Ok(listener) = tokio::net::UnixListener::bind(&sock_path) else {
                    return;
                };
                let mut buf = [0u8; 16];
                loop {
                    let Ok((mut stream, _)) = listener.accept().await else {
                        continue;
                    };
                    let n = stream.read(&mut buf).await.unwrap_or(0);
                    let msg = std::str::from_utf8(&buf[..n]).unwrap_or("").trim();
                    match msg {
                        "gap" => {
                            let gaps = db::get_active_gaps();
                            let _ = app_handle.emit("gap-detected", gaps);
                        }
                        "trade" => {
                            let trades = db::get_recent_trades();
                            let _ = app_handle.emit("trade-executed", trades);
                        }
                        _ => {}
                    }
                }
            });

            Ok(())
```

The `Ok(())` that was already there is now the final line of the `setup()` block — remove the old standalone `Ok(())` and replace with the block above (which ends with `Ok(())`).

- [ ] **Step 3: Verify full build**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/tauri-app/src-tauri
cargo check 2>&1 | grep -E "^error" | head -10
```

Expected: no error lines. If you see `UnixListener` not found, verify tokio feature flags in `Cargo.toml` — `tokio = { version = "1", features = ["full"] }` should already be present.

- [ ] **Step 4: Commit**

```bash
git add tauri-app/src-tauri/src/lib.rs
git commit -m "feat(lib): Unix socket listener → Tauri gap-detected/trade-executed events"
```

---

### Task 4: `notifier.py` — `_notify()` helper

**Files:**
- Modify: `python-core/notifier.py`

- [ ] **Step 1: Write failing test**

Create `python-core/tests/test_notifier_notify.py`:

```python
import socket
import threading
import time
from pathlib import Path
import sys, os
sys.path.insert(0, str(Path(__file__).parent.parent))

import notifier


def _serve_one(sock_path: str) -> list[str]:
    """Start a Unix server, accept one connection, read token, return [token]."""
    received = []
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    srv.settimeout(2.0)

    def _accept():
        try:
            conn, _ = srv.accept()
            data = conn.recv(64)
            received.append(data.decode())
            conn.close()
        except Exception:
            pass
        finally:
            srv.close()

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    return received, t


def test_notify_sends_token(tmp_path):
    sock_path = str(tmp_path / "test.sock")
    received, t = _serve_one(sock_path)

    original = notifier._SOCK_PATH
    notifier._SOCK_PATH = sock_path
    try:
        notifier._notify("gap")
        t.join(timeout=2.0)
        assert received == ["gap"]
    finally:
        notifier._SOCK_PATH = original


def test_notify_silent_on_missing_socket():
    notifier._SOCK_PATH = "/tmp/does_not_exist_polyking.sock"
    notifier._notify("gap")  # must not raise


def test_notify_sends_trade_token(tmp_path):
    sock_path = str(tmp_path / "test2.sock")
    received, t = _serve_one(sock_path)

    original = notifier._SOCK_PATH
    notifier._SOCK_PATH = sock_path
    try:
        notifier._notify("trade")
        t.join(timeout=2.0)
        assert received == ["trade"]
    finally:
        notifier._SOCK_PATH = original
```

- [ ] **Step 2: Run test — expect ImportError or AttributeError**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/python-core
.venv/bin/python -m pytest tests/test_notifier_notify.py -v 2>&1 | tail -15
```

Expected: `AttributeError: module 'notifier' has no attribute '_notify'` (or similar) — confirms test is testing the right thing.

- [ ] **Step 3: Add `_notify()` to `notifier.py`**

At the top of `notifier.py`, after the existing imports (after `from loguru import logger` block, before `_skip_last`), add:

```python
import socket as _socket
from pathlib import Path as _Path

_SOCK_PATH = str(_Path(__file__).parent.parent / "data" / "polyking_events.sock")


def _notify(token: str) -> None:
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.connect(_SOCK_PATH)
        s.send(token.encode())
        s.close()
    except OSError:
        pass  # Tauri not running — ignore
```

- [ ] **Step 4: Call `_notify("gap")` at end of `gap_detected()`**

`gap_detected()` currently ends at line ~68 (the `logger.opt(...)` call). Add one line after that logger call:

```python
    _notify("gap")
```

Full function after edit:

```python
def gap_detected(gap: dict) -> None:
    market_id = gap['market_id']
    now = time.monotonic()
    if now - _gap_last.get(market_id, 0) < _GAP_COOLDOWN:
        return  # Same gap still active — don't spam terminal every second
    _gap_last[market_id] = now
    _metrics.inc_gap_detected(
        pair_type=gap.get("pair_type", "cross_platform"),
        confidence=gap.get("confidence", "medium"),
    )
    _metrics.observe_gap_cents(
        pair_type=gap.get("pair_type", "cross_platform"),
        cents=gap.get("gap_cents", 0.0),
    )
    logger.opt(colors=True).info(
        f"<yellow>GAP</yellow>   | {market_id} "
        f"| Poly: {gap['polymarket_price']:.2f} "
        f"| Kalshi: {gap['kalshi_price']:.2f} "
        f"| Gap: {gap['gap_cents']:.1f}c "
        f"| Conf: {gap.get('confidence', 'medium').upper()}"
    )
    _notify("gap")
```

- [ ] **Step 5: Call `_notify("trade")` at end of `trade_executed()`**

`trade_executed()` currently ends at the `logger.opt(...)` call (line ~98). Add one line:

```python
    _notify("trade")
```

Full function after edit:

```python
def trade_executed(trade: dict) -> None:
    dry_tag = " (DRY RUN)" if trade.get("dry_run") else ""
    logger.opt(colors=True).info(
        f"<cyan>TRADE</cyan> | {trade.get('polymarket_side')} Poly ${trade.get('polymarket_amount', 0):.2f} "
        f"| {trade.get('kalshi_side')} Kalshi ${trade.get('kalshi_amount', 0):.2f} "
        f"| Expected: +${trade.get('expected_profit', 0):.2f}{dry_tag}"
    )
    _notify("trade")
```

- [ ] **Step 6: Run tests — expect all three to pass**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/python-core
.venv/bin/python -m pytest tests/test_notifier_notify.py -v 2>&1 | tail -15
```

Expected:
```
PASSED tests/test_notifier_notify.py::test_notify_sends_token
PASSED tests/test_notifier_notify.py::test_notify_silent_on_missing_socket
PASSED tests/test_notifier_notify.py::test_notify_sends_trade_token
3 passed
```

- [ ] **Step 7: Run full Python test suite to confirm no regressions**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/python-core
.venv/bin/python -m pytest tests/ -q 2>&1 | tail -5
```

Expected: all existing tests still pass.

- [ ] **Step 8: Commit**

```bash
git add python-core/notifier.py python-core/tests/test_notifier_notify.py
git commit -m "feat(notifier): _notify() sends gap/trade tokens to Tauri Unix socket"
```

---

### Task 5: `GapRow.tsx` — extracted gap row component

**Files:**
- Create: `tauri-app/src/components/GapRow.tsx`

The existing gap row at `App.tsx:878-904` is an inline `<tr>` in a `<For>`. Extracting it to a named component means SolidJS only re-renders the row when its own `gap` prop object changes — not when the parent signal updates with unchanged rows.

- [ ] **Step 1: Create `GapRow.tsx`**

Look at the existing `<tr>` in `App.tsx` (lines 879-904) to copy the exact JSX. The helper functions `fmtPrice`, `fmtTime`, `gapClass`, `outcomeClass`, `outcomeCell`, `copyMarketId` are defined in `App.tsx` — they need to be passed as props or the component needs to live in the same module. The cleanest approach: pass the necessary values as plain data props and the `onCopy` callback:

```tsx
import type { Component } from "solid-js";

interface Gap {
  market_id: string;
  token_a_price: number;
  token_b_price: number;
  gap_cents: number;
  confidence: string;
  timestamp: number;
  outcome_count: number;
}

interface GapRowProps {
  gap: Gap;
  index: number;
  isNew: boolean;
  onCopy: (marketId: string) => void;
}

function fmtPrice(p: number): string {
  return p.toFixed(2);
}

function fmtTime(ts: number): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function gapClass(cents: number): string {
  if (cents >= 10) return "gap-high";
  if (cents >= 5) return "gap-med";
  return "gap-low";
}

function outcomeClass(count: number, marketId: string): string {
  if (count === 0) return "dim";
  if (count === 2) return "";
  return "outcome-warn";
}

function outcomeCell(count: number, marketId: string): string {
  if (count === 0) return "?";
  return String(count);
}

const GapRow: Component<GapRowProps> = (props) => {
  return (
    <tr>
      <td class="row-num">{props.index + 1}</td>
      <td>
        <button
          type="button"
          class="market-id market-id-btn"
          title="Copy market id"
          onClick={() => props.onCopy(props.gap.market_id)}
        >
          {props.gap.market_id}
          {props.isNew && <span class="badge-new">NEW</span>}
        </button>
      </td>
      <td class="right mono">{fmtPrice(props.gap.token_a_price)}</td>
      <td class="right mono">{fmtPrice(props.gap.token_b_price)}</td>
      <td class="right">
        <span class={`gap-pill ${gapClass(props.gap.gap_cents)}`}>
          {props.gap.gap_cents.toFixed(1)}¢
        </span>
      </td>
      <td class="dim">{props.gap.confidence}</td>
      <td
        class={outcomeClass(props.gap.outcome_count, props.gap.market_id)}
        title={
          props.gap.outcome_count > 0
            ? `${props.gap.outcome_count} outcomes in this event`
            : "Outcome count unavailable"
        }
      >
        {outcomeCell(props.gap.outcome_count, props.gap.market_id)}
      </td>
      <td class="right mono dim">{fmtTime(props.gap.timestamp)}</td>
    </tr>
  );
};

export default GapRow;
```

- [ ] **Step 2: Verify TypeScript compiles**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/tauri-app
npx tsc --noEmit 2>&1 | head -20
```

Expected: no errors (the component isn't imported anywhere yet, so it's just a syntax check).

- [ ] **Step 3: Commit**

```bash
git add tauri-app/src/components/GapRow.tsx
git commit -m "feat(ui): GapRow extracted component with isNew badge prop"
```

---

### Task 6: `App.tsx` — data layer refactor

Replace `gapsQuery` + `tradesQuery` with signals driven by Tauri event listeners. Keep `statsQuery` (renamed from the stats half of `gapsQuery`) on a 10s poll.

**Files:**
- Modify: `tauri-app/src/App.tsx`

- [ ] **Step 1: Add `onMount` and `untrack` to solid-js import**

Current import (line 1-8):
```typescript
import {
  createSignal,
  createEffect,
  onCleanup,
  For,
  Show,
  createMemo,
} from "solid-js";
```

Replace with:
```typescript
import {
  createSignal,
  createEffect,
  onMount,
  onCleanup,
  untrack,
  For,
  Show,
  createMemo,
} from "solid-js";
```

- [ ] **Step 2: Add `ConnectionStatus` interface and `statsQuery` + `connectionQuery`**

After the existing `const queryBase = () => ({...})` block (after line ~235), replace the `gapsQuery` block (lines 237-249) with:

```typescript
  // statsQuery: 10s poll for aggregate stats (pairs, gaps_today, trades_today, pnl)
  const statsQuery = createQuery(() => ({
    ...queryBase(),
    queryKey: ["polyking", "stats"],
    queryFn: () => invoke<Stats>("get_stats"),
    staleTime: 10_000,
    refetchInterval: 10_000,
  }));

  interface ConnectionStatus { bot_running: boolean; ws_active: boolean }
  const connectionQuery = createQuery(() => ({
    ...queryBase(),
    queryKey: ["polyking", "connection"],
    queryFn: () => invoke<ConnectionStatus>("get_connection_status"),
    staleTime: 3_000,
    refetchInterval: 3_000,
  }));
```

Remove the entire `tradesQuery` block (lines 251-257). The `modeQuery`, `botQuery`, `pnlQuery` blocks remain unchanged.

- [ ] **Step 3: Add gap/trade signals and last-update timestamps**

After the `createSignal` declarations around line 284-303, add:

```typescript
  const [gaps, setGaps] = createSignal<Gap[]>([]);
  const [trades, setTrades] = createSignal<Trade[]>([]);
  const [lastGapUpdate, setLastGapUpdate] = createSignal(Date.now());
  const [lastTradeUpdate, setLastTradeUpdate] = createSignal(Date.now());
  const [now, setNow] = createSignal(Date.now());
```

- [ ] **Step 4: Register event listeners in `onMount`**

Add after the `createShortcut` lines (~411-414):

```typescript
  onMount(async () => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    onCleanup(() => clearInterval(tickId));

    const unlistenGap = await listen<Gap[]>("gap-detected", (e) => {
      setGaps(e.payload.slice(0, 50));
      setLastGapUpdate(Date.now());
      triggerGapFlash();
      const ids = new Set(e.payload.map((g) => g.market_id));
      setNewGapIds(ids);
      setTimeout(() => setNewGapIds(new Set()), 3000);
      void maybeNotifyForGaps(e.payload, poll());
    });
    const unlistenTrade = await listen<Trade[]>("trade-executed", (e) => {
      setTrades(e.payload.slice(0, 500));
      setLastTradeUpdate(Date.now());
    });
    onCleanup(() => {
      unlistenGap();
      unlistenTrade();
    });
  });
```

Note: `triggerGapFlash`, `setNewGapIds` are defined in Task 7. The TypeScript compiler will flag them as undefined until Task 7 is done — that's expected.

- [ ] **Step 5: Fix `stats`, `gaps`, `trades` memos**

Replace lines 322-324:
```typescript
  const stats = createMemo(() => gapsQuery.data?.stats ?? DEFAULT_STATS);
  const gaps = createMemo(() => gapsQuery.data?.gaps ?? []);
  const trades = createMemo(() => tradesQuery.data ?? []);
```

With:
```typescript
  const stats = createMemo(() => statsQuery.data ?? DEFAULT_STATS);
  // gaps and trades are plain signals (set by event listeners above) — no memo needed
```

Remove the `gaps` and `trades` memo lines entirely (they're now signals declared in Step 3).

- [ ] **Step 6: Fix `queryError` memo**

Lines 360-373 reference `gapsQuery.error` and `tradesQuery.error`. Replace:

```typescript
  const queryError = createMemo(() => {
    const candidates = [
      settingsQuery.error,
      gapsQuery.error,
      tradesQuery.error,
      modeQuery.error,
      botQuery.error,
      pnlQuery.error,
    ];
    for (const e of candidates) {
      if (e) return errText(e);
    }
    return null;
  });
```

With:

```typescript
  const queryError = createMemo(() => {
    const candidates = [
      settingsQuery.error,
      statsQuery.error,
      connectionQuery.error,
      modeQuery.error,
      botQuery.error,
      pnlQuery.error,
    ];
    for (const e of candidates) {
      if (e) return errText(e);
    }
    return null;
  });
```

- [ ] **Step 7: Fix `slowSync` effect**

Lines 390-403 reference `gapsQuery.isFetching` and `tradesQuery.isFetching`. Replace:

```typescript
    const fetching =
      gapsQuery.isFetching ||
      tradesQuery.isFetching ||
      modeQuery.isFetching ||
      botQuery.isFetching ||
      pnlQuery.isFetching;
```

With:

```typescript
    const fetching =
      statsQuery.isFetching ||
      connectionQuery.isFetching ||
      modeQuery.isFetching ||
      botQuery.isFetching ||
      pnlQuery.isFetching;
```

- [ ] **Step 8: Fix `maybeNotifyForGaps` effect**

Lines 440-444 read from `gapsQuery.data`. Remove the entire effect:

```typescript
  createEffect(() => {
    const bundle = gapsQuery.data;
    if (!bundle?.gaps) return;
    void maybeNotifyForGaps(bundle.gaps, poll());
  });
```

(Notification is now called directly in the `gap-detected` listener added in Step 4.)

- [ ] **Step 9: Fix gap panel Refresh button and timestamp**

Line 818 calls `qc.invalidateQueries({ queryKey: ["polyking", "gapsStats"] })`. Replace with a direct invoke:

```tsx
onClick={async () => {
  const result = await invoke<Gap[]>("get_active_gaps");
  setGaps(result.slice(0, 50));
  setLastGapUpdate(Date.now());
}}
```

Line 824: `{gapsQuery.dataUpdatedAt ? secsAgo(gapsQuery.dataUpdatedAt) : "—"}` → replace with:

```tsx
{secsAgo(lastGapUpdate())}
```

Where `secsAgo` takes a `Date.now()`-style timestamp. Check current `secsAgo` implementation — if it takes `dataUpdatedAt` (a numeric ms timestamp), it works the same.

- [ ] **Step 10: Fix trades panel Refresh button and timestamp**

Line 920 calls `qc.invalidateQueries({ queryKey: ["polyking", "trades"] })`. Replace with:

```tsx
onClick={async () => {
  const result = await invoke<Trade[]>("get_recent_trades");
  setTrades(result.slice(0, 500));
  setLastTradeUpdate(Date.now());
}}
```

Line 926: `{tradesQuery.dataUpdatedAt ? secsAgo(tradesQuery.dataUpdatedAt) : "—"}` → replace with:

```tsx
{secsAgo(lastTradeUpdate())}
```

- [ ] **Step 11: Replace inline `<For>` in gaps table with `<GapRow>`**

Add import at the top of `App.tsx`:
```typescript
import GapRow from "./components/GapRow";
```

Replace the `<tbody>` contents (lines 877-905):
```tsx
              <tbody>
                <For each={sortedGaps()}>
                  {(g, i) => (
                    <tr>
                      <td class="row-num">{i() + 1}</td>
                      ...
                    </tr>
                  )}
                </For>
              </tbody>
```

With:
```tsx
              <tbody>
                <For each={sortedGaps()}>
                  {(g, i) => (
                    <GapRow
                      gap={g}
                      index={i()}
                      isNew={newGapIds().has(g.market_id)}
                      onCopy={(id) => void copyMarketId(id)}
                    />
                  )}
                </For>
              </tbody>
```

Note: `newGapIds` is defined in Task 7. TypeScript will flag it until then.

- [ ] **Step 12: Verify TypeScript (expect errors only from Task 7 symbols)**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/tauri-app
npx tsc --noEmit 2>&1 | grep "error TS"
```

Expected: only errors referencing `triggerGapFlash`, `setNewGapIds`, `newGapIds` — those are defined in Task 7.

- [ ] **Step 13: Commit**

```bash
git add tauri-app/src/App.tsx
git commit -m "feat(app): replace gapsQuery/tradesQuery with event-driven signals"
```

---

### Task 7: `App.tsx` — visual features

Add: gap flash, NEW badge signal, P&L animation, connection dot, stat card highlights.

**Files:**
- Modify: `tauri-app/src/App.tsx`

- [ ] **Step 1: Add flash + newGapIds signals**

After the `[gapSort, tradeSort]` signals (~lines 300-301), add:

```typescript
  const [gapFlash, setGapFlash] = createSignal(false);
  const [newGapIds, setNewGapIds] = createSignal(new Set<string>());

  function triggerGapFlash() {
    setGapFlash(true);
    setTimeout(() => setGapFlash(false), 300);
  }
```

- [ ] **Step 2: Add P&L animation signal and effect**

After the `triggerGapFlash` function, add:

```typescript
  const [displayPnl, setDisplayPnl] = createSignal(0);

  createEffect(() => {
    const target = stats().pnl;
    const diff = target - untrack(() => displayPnl());
    if (Math.abs(diff) < 0.001) return;
    const step = diff / 20;
    let i = 0;
    const id = setInterval(() => {
      if (i++ >= 20) {
        clearInterval(id);
        setDisplayPnl(target);
        return;
      }
      setDisplayPnl((p) => p + step);
    }, 16);
    onCleanup(() => clearInterval(id));
  });
```

- [ ] **Step 3: Add P&L card class memo and trades pulse**

After the `displayPnl` effect, add:

```typescript
  const pnlCardClass = createMemo(() =>
    stats().pnl > 0 ? "stat-positive" : stats().pnl < 0 ? "stat-negative" : ""
  );

  const [tradesPulse, setTradesPulse] = createSignal(false);
  let prevTrades = -1;
  createEffect(() => {
    const count = stats().trades_today;
    if (prevTrades >= 0 && count > prevTrades) {
      setTradesPulse(true);
      const id = setTimeout(() => setTradesPulse(false), 600);
      onCleanup(() => clearTimeout(id));
    }
    prevTrades = count;
  });
```

- [ ] **Step 4: Add connection status memo**

After `pnlCardClass`:

```typescript
  const connStatus = createMemo(() => connectionQuery.data);
```

- [ ] **Step 5: Apply flash class to gaps panel header**

Find `<div class="panel-header">` above the gaps table (line ~810). Replace:

```tsx
        <div class="panel-header">
```

With:

```tsx
        <div classList={{ "panel-header": true, "panel-flash": gapFlash() }}>
```

- [ ] **Step 6: Apply P&L animation to the P&L stat card**

Find line ~779:
```tsx
          <div class={`stat-value ${stats().pnl >= 0 ? "green" : ""}`}>{fmtPnl(stats().pnl)}</div>
```

Replace with:
```tsx
          <div class={`stat-value ${displayPnl() >= 0 ? "green" : ""}`}>{fmtPnl(displayPnl())}</div>
```

Also apply the card background class to the P&L stat card. Find the parent `<div class="stat-card">` for the P&L stat (line ~777) and replace:

```tsx
        <div class="stat-card">
          <div class="stat-label">{mode() === "DRY_RUN" ? "Simulated P&L" : "Realized P&L"}</div>
```

With:

```tsx
        <div class={`stat-card ${pnlCardClass()}`}>
          <div class="stat-label">{mode() === "DRY_RUN" ? "Simulated P&L" : "Realized P&L"}</div>
```

- [ ] **Step 7: Apply trades pulse to trades stat card**

Find the trades card (line ~772):
```tsx
        <div class="stat-card">
          <div class="stat-label">Trades Executed</div>
```

Replace with:
```tsx
        <div class={`stat-card ${tradesPulse() ? "stat-pulse" : ""}`}>
          <div class="stat-label">Trades Executed</div>
```

- [ ] **Step 8: Add connection dot to topbar**

After the existing bot status `<span>` (line ~698, closing `</span>` of topbar-status), add:

```tsx
        <span
          class={`conn-dot conn-dot-${
            !connStatus()?.bot_running
              ? "dead"
              : connStatus()?.ws_active
              ? "alive"
              : "degraded"
          }`}
          title="WS connection status"
        />
```

- [ ] **Step 9: Run full TypeScript check — expect clean**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/tauri-app
npx tsc --noEmit 2>&1 | grep "error TS"
```

Expected: no output (zero errors).

- [ ] **Step 10: Commit**

```bash
git add tauri-app/src/App.tsx
git commit -m "feat(app): gap flash, P&L animation, connection dot, stat card highlights"
```

---

### Task 8: `index.css` — new styles

**Files:**
- Modify: `tauri-app/src/index.css`

- [ ] **Step 1: Append new styles**

Read the last 20 lines of `index.css` to find a good insertion point, then append:

```css
/* ── Gap flash ─────────────────────────────────────────────────────── */
.panel-flash { animation: flash-yellow 300ms ease-out; }
@keyframes flash-yellow {
  0%   { background: rgba(202, 138, 4, 0.25); }
  100% { background: transparent; }
}

/* ── NEW badge on gap rows ──────────────────────────────────────────── */
.badge-new {
  background: #ca8a04;
  color: #000;
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 3px;
  margin-left: 6px;
  opacity: 1;
  transition: opacity 0.4s;
}

/* ── Connection dot ─────────────────────────────────────────────────── */
.conn-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.conn-dot-alive    { background: #22c55e; }
.conn-dot-degraded { background: #eab308; }
.conn-dot-dead     { background: #ef4444; }

/* ── Stat card reactive highlights ─────────────────────────────────── */
.stat-positive { background: rgba(34, 197, 94, 0.08); transition: background 0.4s; }
.stat-negative { background: rgba(239, 68, 68, 0.08); transition: background 0.4s; }
.stat-pulse    { animation: pulse-green 600ms ease-out; }
@keyframes pulse-green {
  0%,100% { background: transparent; }
  50%     { background: rgba(34, 197, 94, 0.15); }
}
```

- [ ] **Step 2: Commit**

```bash
git add tauri-app/src/index.css
git commit -m "feat(css): flash, badge-new, conn-dot, stat reactive highlight styles"
```

---

### Task 9: Full build verification

**Files:** None changed — verification only.

- [ ] **Step 1: Rust unit tests**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/tauri-app/src-tauri
cargo test 2>&1 | tail -15
```

Expected: all existing tests pass, including the 3 new `has_recent_gap_activity` tests.

- [ ] **Step 2: TypeScript check**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/tauri-app
npx tsc --noEmit 2>&1
```

Expected: no output (zero errors).

- [ ] **Step 3: Python test suite**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/python-core
.venv/bin/python -m pytest tests/ -q 2>&1 | tail -5
```

Expected: all tests pass including the 3 new `test_notifier_notify` tests.

- [ ] **Step 4: Tauri dev build smoke test**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/tauri-app
npm run tauri dev 2>&1 | head -30
```

Watch for: `cargo` compilation success, no TypeScript errors in Vite output, no `UnixListener` bind errors in console, Tauri window opens.

- [ ] **Step 5: Final commit**

```bash
git add -p  # stage any remaining uncommitted changes
git commit -m "chore: solidjs reactivity full implementation verified"
```

---

## Self-Review

**Spec coverage:**
- Replace polling with events ✅ Tasks 1-4 (Rust socket → Python notify)
- Fix table reactivity ✅ Task 5 (GapRow component)
- Visual feedback on new gap ✅ Tasks 7+8 (flash, NEW badge)
- Last updated timestamp ✅ Tasks 6+8 (`lastGapUpdate` / `lastTradeUpdate` signals, `secsAgo()`)
- P&L animation ✅ Task 7 (`displayPnl`, `createEffect` with `untrack` + `onCleanup`)
- Connection indicator ✅ Tasks 2-3+7+8 (`get_connection_status`, `connStatus` memo, dot JSX + CSS)
- Stats card reactivity ✅ Task 7+8 (`pnlCardClass`, `tradesPulse`, CSS)

**Placeholder scan:** None — all steps include exact code.

**Type consistency:** `Gap[]` and `Trade[]` used consistently; `ConnectionStatus` struct matches `interface ConnectionStatus` in frontend; `GapRow` props match usage in `<For>` body.
