# Moulti Terminal Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a terminal-only monitoring dashboard using Moulti that routes `main.py` log output into 5 labeled, color-coded sections (status, gaps, trades, rust_feed, errors) with a live stats header.

**Architecture:** A Python router script (`scripts/moulti_router.py`) is invoked by `moulti run`, initializes 5 moulti steps, spawns `main.py` as a subprocess, routes each output line to the correct step via persistent OS pipes, and runs a background thread that polls `.env` + SQLite every 3 s to update the pinned status header via `--top-text`. A thin bash launcher (`scripts/terminal_ui.sh`) handles dependency checks and the `moulti run` invocation. `start.sh` gains a `--moulti` flag and `MOULTI=1` env check that replaces step 5 (bot launch) with the terminal UI.

**Tech Stack:** Python 3.11+ (stdlib only: `os`, `re`, `signal`, `sqlite3`, `subprocess`, `threading`, `time`), Moulti 1.34.1 (installed via pipx), bash, loguru (already in venv for ANSI output)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `scripts/moulti_router.py` | Create | Step init, subprocess spawn, line routing, status poller |
| `scripts/tests/test_moulti_router.py` | Create | Unit tests for pure functions |
| `scripts/terminal_ui.sh` | Create | Dependency check + `moulti run` invocation |
| `start.sh` | Modify | `--moulti` flag / `MOULTI=1` detection |

---

## Task 1: Failing tests for pure functions

**Files:**
- Create: `scripts/tests/test_moulti_router.py`

- [ ] **Step 1: Create the test file**

```python
# scripts/tests/test_moulti_router.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # adds scripts/ to path

from moulti_router import (
    strip_ansi,
    classify_line,
    colorize_line,
    format_status_header,
)

RESET = '\033[0m'
GREEN = '\033[32m'
RED = '\033[31m'
BOLD_RED = '\033[1;31m'
YELLOW = '\033[33m'


def test_strip_ansi_removes_color_codes():
    assert strip_ansi('\033[32mhello\033[0m') == 'hello'
    assert strip_ansi('no codes') == 'no codes'
    assert strip_ansi('\033[1;31mbold red\033[0m text') == 'bold red text'


# classify_line ──────────────────────────────────────────────────────────────

def test_classify_gap_plain():
    assert classify_line('[10:02:11] INFO  | GAP   | KXBTCD | Gap: 7.2c') == 'gaps'


def test_classify_gap_with_ansi():
    # loguru embeds ANSI — strip_ansi must be applied before matching
    assert classify_line('[10:02:11] INFO  | \033[33mGAP\033[0m   | KXBTCD') == 'gaps'


def test_classify_trade():
    assert classify_line('[10:03:44] INFO  | TRADE | YES Poly $10.00 | Expected: +$0.72') == 'trades'


def test_classify_rust():
    assert classify_line('[10:01:35] DEBUG | [rust] snapshot received') == 'rust_feed'


def test_classify_error():
    assert classify_line('[10:05:00] ERROR | something failed') == 'errors'


def test_classify_warning():
    assert classify_line('[10:05:00] WARNING | WebSocket disconnected: kalshi') == 'errors'


def test_classify_status_fallback():
    assert classify_line('[10:01:33] INFO  | Bot started. Mode=DRY RUN') == 'status'
    assert classify_line('[10:01:33] INFO  | WebSocket connected: kalshi') == 'status'
    assert classify_line('[10:01:33] INFO  | 80802 market pairs loaded') == 'status'


# colorize_line ──────────────────────────────────────────────────────────────

def test_colorize_trade_profit_green():
    line = 'TRADE | YES Poly $10.00 | Expected: +$0.72'
    result = colorize_line(line, 'trades')
    assert result.startswith(GREEN)
    assert result.endswith(RESET)
    assert line in result


def test_colorize_trade_loss_red():
    line = 'TRADE | YES Poly $10.00 | Expected: -$0.30'
    result = colorize_line(line, 'trades')
    assert result.startswith(RED)
    assert result.endswith(RESET)


def test_colorize_error_bold_red():
    result = colorize_line('ERROR | connection failed', 'errors')
    assert result.startswith(BOLD_RED)
    assert result.endswith(RESET)


def test_colorize_warning_yellow():
    result = colorize_line('WARNING | WebSocket disconnected', 'errors')
    assert result.startswith(YELLOW)
    assert result.endswith(RESET)


def test_colorize_gaps_passthrough():
    line = '\033[33mGAP\033[0m   | KXBTCD | Gap: 7.2c'
    assert colorize_line(line, 'gaps') == line  # loguru color already present


def test_colorize_rust_passthrough():
    line = 'DEBUG | [rust] snapshot received'
    assert colorize_line(line, 'rust_feed') == line


def test_colorize_status_passthrough():
    line = 'Bot started. Mode=DRY RUN'
    assert colorize_line(line, 'status') == line


# format_status_header ───────────────────────────────────────────────────────

def test_format_status_header_content():
    result = format_status_header('DRY RUN', 80802, 271, 12, 3)
    assert 'DRY RUN' in result
    assert '80,802' in result
    assert '00:04:31' in result
    assert '12' in result
    assert '3' in result


def test_format_status_header_live_mode():
    result = format_status_header('LIVE', 80802, 3661, 0, 0)
    assert 'LIVE' in result
    assert '01:01:01' in result


def test_format_status_header_fallback_strings():
    # pairs/gaps/trades are '—' when DB is unreachable
    result = format_status_header('DRY RUN', '—', 0, '—', '—')
    assert '—' in result
    assert '00:00:00' in result


def test_format_status_header_single_line():
    # Must fit on one line (no newlines) — used as moulti --top-text
    result = format_status_header('DRY RUN', 80802, 60, 5, 1)
    assert '\n' not in result
```

- [ ] **Step 2: Verify tests fail (moulti_router.py doesn't exist yet)**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
python -m pytest scripts/tests/test_moulti_router.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'moulti_router'`

---

## Task 2: Pure functions — make tests pass

**Files:**
- Create: `scripts/moulti_router.py` (pure functions only at this stage)

- [ ] **Step 1: Create moulti_router.py with pure functions**

```python
#!/usr/bin/env python3
"""moulti_router.py — PolyyKing terminal dashboard router.

Invoked by terminal_ui.sh via:
    moulti run -- python scripts/moulti_router.py /path/to/repo
"""

import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent
DB_PATH = REPO / "data" / "trades.db"
ENV_PATH = REPO / "config" / ".env"
VENV_PYTHON = REPO / "python-core" / ".venv" / "bin" / "python"
MAIN_PY = REPO / "python-core" / "main.py"

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')


# ── Pure helpers (unit-tested) ─────────────────────────────────────────────

def strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences from a string."""
    return ANSI_ESCAPE.sub('', s)


def classify_line(line: str) -> str:
    """Map a log line to a moulti step name based on content."""
    plain = strip_ansi(line)
    if 'GAP' in plain:
        return 'gaps'
    if 'TRADE' in plain:
        return 'trades'
    if '[rust]' in plain:
        return 'rust_feed'
    if 'ERROR' in plain or 'WARNING' in plain:
        return 'errors'
    return 'status'


def colorize_line(line: str, step: str) -> str:
    """Wrap line in ANSI codes for its step.

    gaps / rust_feed / status: pass through unchanged (loguru already colorized).
    trades: green if profit (+$), red if loss.
    errors: bold red for ERROR, yellow for WARNING.
    """
    plain = strip_ansi(line)
    if step == 'trades':
        if '+$' in plain:
            return f'\033[32m{line}\033[0m'
        return f'\033[31m{line}\033[0m'
    if step == 'errors':
        if 'ERROR' in plain:
            return f'\033[1;31m{line}\033[0m'
        return f'\033[33m{line}\033[0m'
    return line


def format_status_header(mode: str, pairs, uptime_secs: int, gaps_today, trades_today) -> str:
    """Single-line string used as moulti --top-text for the status step."""
    h, rem = divmod(int(uptime_secs), 3600)
    m, s = divmod(rem, 60)
    pairs_str = f'{pairs:,}' if isinstance(pairs, int) else str(pairs)
    return (
        f'Mode: {mode}  |  Pairs: {pairs_str}  |  Uptime: {h:02d}:{m:02d}:{s:02d}'
        f'  |  Gaps today: {gaps_today}  |  Trades: {trades_today}'
    )


def read_mode_from_env(env_path: Path) -> str:
    """Read DRY_RUN flag from .env; returns 'LIVE' or 'DRY RUN'."""
    try:
        for line in env_path.read_text().splitlines():
            if line.startswith('DRY_RUN='):
                val = line.split('=', 1)[1].strip().strip('"').lower()
                return 'LIVE' if val == 'false' else 'DRY RUN'
    except OSError:
        pass
    return 'DRY RUN'


def read_status_from_db(db_path: Path) -> dict:
    """Query SQLite for live stats; returns safe fallback strings on any error."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=1.0)
        today = time.strftime('%Y-%m-%d')
        pairs = conn.execute('SELECT COUNT(*) FROM market_pairs').fetchone()[0]
        gaps = conn.execute(
            'SELECT COUNT(*) FROM gaps WHERE detected_at >= ?', (today,)
        ).fetchone()[0]
        trades = conn.execute(
            'SELECT COUNT(*) FROM trades WHERE opened_at >= ?', (today,)
        ).fetchone()[0]
        conn.close()
        return {'pairs': pairs, 'gaps_today': gaps, 'trades_today': trades}
    except Exception:
        return {'pairs': '—', 'gaps_today': '—', 'trades_today': '—'}


if __name__ == '__main__':
    print("moulti_router: pure functions loaded OK. Run via terminal_ui.sh.")
```

- [ ] **Step 2: Run tests — expect all green**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
python -m pytest scripts/tests/test_moulti_router.py -v
```

Expected output (all pass):
```
test_strip_ansi_removes_color_codes PASSED
test_classify_gap_plain PASSED
test_classify_gap_with_ansi PASSED
test_classify_trade PASSED
test_classify_rust PASSED
test_classify_error PASSED
test_classify_warning PASSED
test_classify_status_fallback PASSED
test_colorize_trade_profit_green PASSED
test_colorize_trade_loss_red PASSED
test_colorize_error_bold_red PASSED
test_colorize_warning_yellow PASSED
test_colorize_gaps_passthrough PASSED
test_colorize_rust_passthrough PASSED
test_colorize_status_passthrough PASSED
test_format_status_header_content PASSED
test_format_status_header_live_mode PASSED
test_format_status_header_fallback_strings PASSED
test_format_status_header_single_line PASSED
19 passed
```

- [ ] **Step 3: Commit**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
git add scripts/moulti_router.py scripts/tests/test_moulti_router.py
git commit -m "feat(moulti): router pure functions + tests (19 green)"
```

---

## Task 3: Add subprocess management to moulti_router.py

**Files:**
- Modify: `scripts/moulti_router.py` — append `_init_steps`, `_start_pass_procs`, `_status_poller`, `main()`

The router uses one OS pipe per step. `moulti pass <step>` reads from the read-end in a background subprocess; the router writes to the write-end. The status step gets a poller thread that calls `moulti step update status --top-text "..."` every 3 s without touching the content area.

- [ ] **Step 1: Replace the `if __name__ == '__main__':` block at the end of moulti_router.py with the full process management code**

Replace:
```python
if __name__ == '__main__':
    print("moulti_router: pure functions loaded OK. Run via terminal_ui.sh.")
```

With:
```python
# ── Process management ─────────────────────────────────────────────────────

_shutdown = threading.Event()

STEPS = ('status', 'gaps', 'trades', 'rust_feed', 'errors')


def _moulti(*args: str) -> None:
    subprocess.run(['moulti', *args], capture_output=True)


def _init_steps(initial_header: str) -> None:
    _moulti('step', 'add', 'status',
            '--title', 'Status',
            '--top-text', initial_header,
            '--auto-scroll')
    _moulti('step', 'add', 'gaps',
            '--title', 'Gaps Detected Today',
            '--classes', 'warning',
            '--auto-scroll')
    _moulti('step', 'add', 'trades',
            '--title', 'Recent Trades',
            '--classes', 'success',
            '--auto-scroll')
    _moulti('step', 'add', 'rust_feed',
            '--title', 'Rust Bridge',
            '--auto-scroll',
            '--collapsed')
    _moulti('step', 'add', 'errors',
            '--title', 'Warnings / Errors',
            '--classes', 'error',
            '--auto-scroll')


def _start_pass_procs(pipes: dict) -> list:
    """Start a `moulti pass <step>` reader process for every step."""
    procs = []
    for step, (r, _) in pipes.items():
        proc = subprocess.Popen(['moulti', 'pass', step], stdin=r, text=True)
        os.close(r)  # router owns write-end; pass proc owns read-end
        procs.append(proc)
    return procs


def _status_poller(start_time: float) -> None:
    """Background thread: refresh status step --top-text every 3 s."""
    while not _shutdown.is_set():
        mode = read_mode_from_env(ENV_PATH)
        db = read_status_from_db(DB_PATH)
        uptime = int(time.monotonic() - start_time)
        header = format_status_header(
            mode, db['pairs'], uptime, db['gaps_today'], db['trades_today']
        )
        _moulti('step', 'update', 'status', '--top-text', header)
        _shutdown.wait(3.0)


def main() -> int:
    start_time = time.monotonic()
    initial_header = format_status_header('DRY RUN', '…', 0, '…', '…')
    _init_steps(initial_header)

    # One OS pipe per step; moulti pass reads the read-end
    pipes = {step: os.pipe() for step in STEPS}
    pass_procs = _start_pass_procs(pipes)
    write_fds = {step: w for step, (_, w) in pipes.items()}

    poller = threading.Thread(target=_status_poller, args=(start_time,), daemon=True)
    poller.start()

    bot = subprocess.Popen(
        [str(VENV_PYTHON), str(MAIN_PY)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(REPO),
    )

    def _shutdown_handler(sig, _frame):
        _shutdown.set()
        bot.terminate()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    for raw_line in bot.stdout:
        step = classify_line(raw_line)
        colored = colorize_line(raw_line, step)
        payload = (colored.rstrip('\n') + '\n').encode()
        try:
            os.write(write_fds[step], payload)
        except OSError:
            pass

    bot.wait()
    _shutdown.set()

    # Close all write-ends so moulti pass processes see EOF and exit cleanly
    for fd in write_fds.values():
        try:
            os.close(fd)
        except OSError:
            pass
    for proc in pass_procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    code = bot.returncode or 0
    if code != 0:
        _moulti('step', 'update', 'errors', '--text',
                f'\033[1;31m[moulti_router] main.py exited with code {code}\033[0m')
    return code


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 2: Verify existing tests still pass (pure functions unchanged)**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
python -m pytest scripts/tests/test_moulti_router.py -v
```

Expected: 19 passed

- [ ] **Step 3: Verify the script at least imports cleanly**

```bash
python scripts/moulti_router.py
```

Expected: prints nothing and exits 0 (the `if __name__ == '__main__'` block calls `main()` which requires moulti to be running — the script will error with a moulti socket message, not a Python import error).

Actually the script will call `main()` which calls `_init_steps()` which calls `moulti step add` — moulti isn't running so those will fail silently (`capture_output=True`), then it'll try to spawn `main.py` which may or may not exist. To just check imports:

```bash
python -c "import sys; sys.argv=['moulti_router.py']; import moulti_router; print('imports OK')"
```

Expected: `imports OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
git add scripts/moulti_router.py
git commit -m "feat(moulti): router subprocess management, pipe routing, status poller"
```

---

## Task 4: Create terminal_ui.sh

**Files:**
- Create: `scripts/terminal_ui.sh`

- [ ] **Step 1: Create the launcher**

```bash
#!/usr/bin/env bash
# terminal_ui.sh — launch PolyyKing bot inside a Moulti terminal dashboard
# Usage: bash scripts/terminal_ui.sh
#   or:  MOULTI=1 bash start.sh
#   or:  bash start.sh --moulti
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# ── Dependency checks ───────────────────────────────────────────────────────
if ! command -v moulti &>/dev/null; then
  echo "[terminal_ui.sh] moulti not found." >&2
  echo "[terminal_ui.sh] Install with: pipx install moulti" >&2
  exit 1
fi

VENV_PYTHON="$REPO/python-core/.venv/bin/python"
if [ ! -f "$VENV_PYTHON" ]; then
  echo "[terminal_ui.sh] Python venv not found at $VENV_PYTHON" >&2
  echo "[terminal_ui.sh] Run: cd '$REPO/python-core' && uv sync" >&2
  exit 1
fi

ROUTER="$REPO/scripts/moulti_router.py"
if [ ! -f "$ROUTER" ]; then
  echo "[terminal_ui.sh] Router script not found: $ROUTER" >&2
  exit 1
fi

# ── Launch ──────────────────────────────────────────────────────────────────
# moulti run wraps the router in a Moulti TUI instance.
# The router receives MOULTI_SOCKET_PATH via the environment
# so all `moulti step` / `moulti pass` calls connect to this instance.
echo "[terminal_ui.sh] Starting PolyyKing terminal dashboard..."
exec moulti run -- "$VENV_PYTHON" "$ROUTER" "$REPO"
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x /Users/sarvadhisolution/Documents/Personal/PolyyKing/scripts/terminal_ui.sh
```

- [ ] **Step 3: Commit**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
git add scripts/terminal_ui.sh
git commit -m "feat(moulti): terminal_ui.sh launcher with dependency checks"
```

---

## Task 5: Modify start.sh + smoke test

**Files:**
- Modify: `start.sh` — insert moulti detection block after `REPO=` line

- [ ] **Step 1: Insert moulti detection block into start.sh**

Open `start.sh`. After the line `REPO="$(cd "$(dirname "$0")" && pwd)"` (line 3), insert this block:

```bash

# ── 0. --moulti flag or MOULTI=1: launch terminal dashboard ─────────────────
for _arg in "$@"; do
  if [ "$_arg" = "--moulti" ]; then
    exec bash "$REPO/scripts/terminal_ui.sh"
  fi
done
if [ "${MOULTI:-0}" = "1" ]; then
  exec bash "$REPO/scripts/terminal_ui.sh"
fi
```

The final `start.sh` section order becomes: 0 (moulti check), 1 (env), 2 (Rust build), 3 (SSL), 4 (backfill), 5 (launch bot).

- [ ] **Step 2: Verify the flag routes correctly without launching (dry-check)**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
bash -c 'REPO=. source start.sh --moulti' 2>&1 | head -5
```

If moulti is installed but venv missing it should print:
```
[terminal_ui.sh] Python venv not found at ...
```
(meaning the routing worked — it reached terminal_ui.sh before aborting)

Or if the venv exists, it opens the moulti TUI.

- [ ] **Step 3: Verify default start.sh is unchanged**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
bash -n start.sh && echo "syntax OK"
```

Expected: `syntax OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
git add start.sh
git commit -m "feat(moulti): start.sh --moulti flag and MOULTI=1 env routing"
```

- [ ] **Step 5: Full smoke test**

With the venv set up and the bot configured, run:

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
bash start.sh --moulti
```

Expected in terminal:
- Moulti TUI opens with 5 labeled sections: Status, Gaps Detected Today, Recent Trades, Rust Bridge (collapsed), Warnings / Errors
- Status top-text shows: `Mode: DRY RUN  |  Pairs: 80,802  |  Uptime: 00:00:xx  |  Gaps today: N  |  Trades: N`
- Bot log lines appear in Status section
- After first GAP event: a line appears in Gaps Detected Today (yellow/warning color)
- After first TRADE event: a line appears in Recent Trades (green if profit, red if loss)
- Ctrl+C gracefully shuts down bot + moulti

---

## Usage Reference (add to README later if desired)

```bash
# Terminal dashboard (Moulti)
bash start.sh --moulti
# or
MOULTI=1 bash start.sh

# Standard (no Moulti)
bash start.sh

# Standalone (skips start.sh setup steps)
bash scripts/terminal_ui.sh
```
