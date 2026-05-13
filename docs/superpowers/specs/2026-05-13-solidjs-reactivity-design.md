# SolidJS Reactivity & Event-Driven UI Design

**Date:** 2026-05-13  
**Status:** Approved

## Goal

Replace 2s polling for gaps and trades with a ~20ms Unix socket event path, and add visual reactivity (flash, animation, connection indicator, card highlights) using SolidJS primitives. No new npm or Cargo dependencies.

## Constraints

- No new dependencies (no XState, no additional npm packages)
- No new Cargo crates (tokio already present)
- Sound toggle dropped (Audio API out of scope without new deps)
- Python changes kept minimal (≤15 lines in notifier.py)

---

## Architecture

### Event flow (new)

```
Python gap_detected()
  └── notifier.py: socket.send("gap\n") → data/polyking_events.sock
          └── Tauri lib.rs tokio task: accept() → re-query SQLite
                  └── app_handle.emit("gap-detected", Vec<Gap>)
                          └── App.tsx listen("gap-detected") → setGaps(payload)
```

Same path for trades: `"trade\n"` → `"trade-executed"` event → `setTrades(payload)`.

### Socket details

- **Path:** `{REPO}/data/polyking_events.sock` (same anchor as DB path: `CARGO_MANIFEST_DIR/../../data/`)
- **Protocol:** newline-terminated ASCII token — `"gap\n"` or `"trade\n"`
- **Python write:** non-blocking `socket.send()` in `try/except`; silently ignored when Tauri is not running
- **Tauri listener:** `tokio::net::UnixListener` bound in `setup()`, runs forever in a `tokio::spawn` task
- **On notification:** Tauri re-queries `db::get_active_gaps()` / `db::get_recent_trades()` and emits the full typed payload — frontend replaces its local list entirely

### Polling that remains (TanStack Query)

| Query | Interval | Command |
|---|---|---|
| `statsQuery` | 10 s | `get_stats` |
| `modeQuery` | 10 s | `get_mode` |
| `botQuery` | 5 s | `get_bot_running` |
| `pnlQuery` | 60 s | `get_daily_pnl` |
| `connectionQuery` | 3 s | `get_connection_status` (new) |

`gapsQuery` and `tradesQuery` are **removed**. Replaced by signals + event listeners.

---

## Rust changes

### `lib.rs` — background socket listener task

Added inside `setup()` after the tray setup, using the already-imported `AppHandle`, `Emitter`, `tokio`:

```rust
let app_handle = app.handle().clone();
tokio::spawn(async move {
    let sock_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent().unwrap().parent().unwrap()
        .join("data/polyking_events.sock");
    let _ = std::fs::remove_file(&sock_path); // remove stale socket on start
    let Ok(listener) = tokio::net::UnixListener::bind(&sock_path) else { return };
    let mut buf = [0u8; 16];
    loop {
        let Ok((mut stream, _)) = listener.accept().await else { continue };
        use tokio::io::AsyncReadExt;
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
```

### `commands.rs` — new `get_connection_status` command

```rust
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ConnectionStatus {
    pub bot_running: bool,
    pub ws_active: bool,  // true if any gap detected in last 30s
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

### `db.rs` — new helper

```rust
pub fn has_recent_gap_activity() -> bool {
    match open() {
        Ok(conn) => conn
            .query_row(
                "SELECT COUNT(*) FROM gaps WHERE detected_at > datetime('now', '-30 seconds')",
                [],
                |r| r.get::<_, i64>(0),
            )
            .unwrap_or(0) > 0,
        Err(_) => false,
    }
}
```

---

## Python changes (`notifier.py`)

Add socket notification after each gap/trade log call. Import `socket` at the top of the file. Add a module-level helper:

```python
import socket as _socket

_SOCK_PATH = str(Path(__file__).parent.parent / "data" / "polyking_events.sock")

def _notify(token: str) -> None:
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.connect(_SOCK_PATH)
        s.send(token.encode())
        s.close()
    except OSError:
        pass  # Tauri not running — ignore
```

Call `_notify("gap")` at end of `gap_detected()`.  
Call `_notify("trade")` at end of `trade_executed()`.

---

## Frontend changes (`App.tsx`)

### Signals replacing removed queries

```typescript
const [gaps, setGaps] = createSignal<Gap[]>([])
const [trades, setTrades] = createSignal<Trade[]>([])
const [lastGapUpdate, setLastGapUpdate] = createSignal(Date.now())
const [lastTradeUpdate, setLastTradeUpdate] = createSignal(Date.now())
const [now, setNow] = createSignal(Date.now())
```

1s ticker in `onMount` to drive "X seconds ago":
```typescript
const tickId = setInterval(() => setNow(Date.now()), 1000)
onCleanup(() => clearInterval(tickId))
```

### Event listeners in `onMount`

```typescript
const unlistenGap = await listen<Gap[]>("gap-detected", (e) => {
  setGaps(e.payload.slice(0, 50))
  setLastGapUpdate(Date.now())
  triggerGapFlash()
})
const unlistenTrade = await listen<Trade[]>("trade-executed", (e) => {
  setTrades(e.payload.slice(0, 500))
  setLastTradeUpdate(Date.now())
})
onCleanup(() => { unlistenGap(); unlistenTrade() })
```

### `statsQuery` replaces `gapsQuery` for stats

```typescript
const statsQuery = createQuery(() => ({
  queryKey: ["polyking", "stats"],
  queryFn: () => invoke<Stats>("get_stats"),
  staleTime: 10_000,
  refetchInterval: 10_000,
}))
```

### `connectionQuery` (new)

```typescript
interface ConnectionStatus { bot_running: boolean; ws_active: boolean }

const connectionQuery = createQuery(() => ({
  queryKey: ["polyking", "connection"],
  queryFn: () => invoke<ConnectionStatus>("get_connection_status"),
  staleTime: 3_000,
  refetchInterval: 3_000,
}))
```

---

## Visual features

### Gap flash + NEW badge

```typescript
const [gapFlash, setGapFlash] = createSignal(false)
const [newGapIds, setNewGapIds] = createSignal(new Set<string>())

function triggerGapFlash() {
  setGapFlash(true)
  setTimeout(() => setGapFlash(false), 300)
}

// In gap event handler, also:
const ids = new Set(payload.map(g => g.market_id))
setNewGapIds(ids)
setTimeout(() => setNewGapIds(new Set()), 3000)
```

Panel header: `classList={{ "panel-header": true, "panel-flash": gapFlash() }}`  
`GapRow` renders `<span class="badge-new">NEW</span>` when `newGapIds().has(gap.market_id)`.

CSS:
```css
.panel-flash { animation: flash-yellow 300ms ease-out; }
@keyframes flash-yellow { 0% { background: #ca8a04; } 100% { background: transparent; } }
.badge-new { 
  background: #ca8a04; color: #000; font-size: 10px; padding: 1px 5px;
  border-radius: 3px; opacity: 1; transition: opacity 0.4s;
}
```

### `GapRow` component

Extracted to `src/components/GapRow.tsx`. Receives `gap: Gap` and `isNew: boolean` as props. SolidJS `<For>` already tracks by reference — rows only re-render when their object changes.

### P&L animation (with `onCleanup`)

```typescript
const [displayPnl, setDisplayPnl] = createSignal(0)

createEffect(() => {
  const target = stats().pnl
  const diff = target - displayPnl()
  if (Math.abs(diff) < 0.001) return
  const step = diff / 20
  let i = 0
  const id = setInterval(() => {
    if (i++ >= 20) { clearInterval(id); setDisplayPnl(target); return }
    setDisplayPnl(p => p + step)
  }, 16)
  onCleanup(() => clearInterval(id))
})
```

`fmtPnl(displayPnl())` used in the P&L stat card instead of `fmtPnl(stats().pnl)`.

### Connection status indicator

Second dot in topbar, next to bot status:
```typescript
const connStatus = createMemo(() => connectionQuery.data)

// Color logic:
// green: bot_running && ws_active
// yellow: bot_running && !ws_active  
// red: !bot_running
```

```tsx
<span class={`conn-dot conn-dot-${
  !connStatus()?.bot_running ? 'dead' :
  connStatus()?.ws_active ? 'alive' : 'degraded'
}`} title="WS connection status" />
```

CSS:
```css
.conn-dot { display:inline-block; width:8px; height:8px; border-radius:50%; }
.conn-dot-alive { background: #22c55e; }
.conn-dot-degraded { background: #eab308; }
.conn-dot-dead { background: #ef4444; }
```

### Stats card reactive highlights

```typescript
const pnlCardClass = createMemo(() =>
  stats().pnl > 0 ? 'stat-positive' :
  stats().pnl < 0 ? 'stat-negative' : ''
)

// Trades pulse: detect increase (skip initial render)
const [tradesPulse, setTradesPulse] = createSignal(false)
let prevTrades = -1
createEffect(() => {
  const count = stats().trades_today
  if (prevTrades >= 0 && count > prevTrades) {
    setTradesPulse(true)
    const id = setTimeout(() => setTradesPulse(false), 600)
    onCleanup(() => clearTimeout(id))
  }
  prevTrades = count
})
```

CSS (transitions, not JS animation):
```css
.stat-positive { background: rgba(34,197,94,0.08); transition: background 0.4s; }
.stat-negative { background: rgba(239,68,68,0.08); transition: background 0.4s; }
.stat-pulse { animation: pulse-green 600ms ease-out; }
@keyframes pulse-green { 0%,100% { background: transparent; } 50% { background: rgba(34,197,94,0.15); } }
```

---

## Files changed

| File | Change |
|---|---|
| `tauri-app/src-tauri/src/lib.rs` | Add tokio socket listener task in `setup()` |
| `tauri-app/src-tauri/src/commands.rs` | Add `get_connection_status()` command + register in `invoke_handler` |
| `tauri-app/src-tauri/src/db.rs` | Add `has_recent_gap_activity()` helper |
| `python-core/notifier.py` | Add `_notify()` helper, call after gap + trade logs |
| `tauri-app/src/App.tsx` | Replace gapsQuery/tradesQuery with signals + listeners; add visual features |
| `tauri-app/src/components/GapRow.tsx` | New — extracted gap row component |
| `tauri-app/src/index.css` | Add flash, badge, conn-dot, stat-positive/negative/pulse styles |

---

## Error handling

- **Socket missing (Python → Rust):** `_notify()` wraps in `try/except OSError` — bot continues normally if Tauri isn't open
- **Rust socket bind fails:** `let Ok(...) else { return }` — task exits silently, app falls back to polling-only
- **Event listener fail:** TanStack Query stats/mode/bot polls continue — gaps/trades just show empty until next event
- **`has_recent_gap_activity` DB error:** returns `false` → connection dot shows yellow (degraded), not crash
