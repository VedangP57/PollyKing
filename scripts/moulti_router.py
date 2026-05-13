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
    """Wrap line in ANSI codes appropriate to its step.

    gaps / rust_feed / status: pass through unchanged (loguru already colorized).
    trades: green if profit line contains +$, red otherwise.
    errors: bold red for ERROR lines, yellow for WARNING lines.
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
