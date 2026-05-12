"""Chaos engineering tests — marked @pytest.mark.chaos to exclude from fast runs.

These tests verify bot behavior after crash, DB corruption, and bridge death.
Run with: uv run pytest tests/test_chaos.py -m chaos -v
"""
import asyncio
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import tracker
from tracker import _create_tables

pytestmark = pytest.mark.chaos


# ── Scenario A: crash between leg placement and fill ──────────────────────────

@pytest.mark.asyncio
async def test_startup_audit_detects_orphan_after_simulated_crash():
    """startup_audit finds an exchange position that has no matching trade in DB.

    Simulates the scenario where the bot placed leg_a on Polymarket but was
    SIGKILL'd before log_trade() ran. On restart, startup_audit should detect
    the orphan and insert it into emergency_positions.
    """
    from startup_audit import audit_orphan_positions

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = tracker.init_db(db_path)

    orphan_asset_id = "orphan-poly-asset-abc123"
    orphan_size = 25.0

    # Exchange reports an open position that isn't in trades.db
    mock_poly = AsyncMock()
    mock_poly.get_open_positions.return_value = [
        {"asset_id": orphan_asset_id, "size": orphan_size, "outcome": "YES"}
    ]
    mock_kalshi = AsyncMock()
    mock_kalshi.get_open_orders.return_value = []

    await audit_orphan_positions(mock_poly, mock_kalshi, conn)

    row = conn.execute(
        "SELECT * FROM emergency_positions WHERE order_id=?", (orphan_asset_id,)
    ).fetchone()
    assert row is not None, (
        "startup_audit must insert orphan into emergency_positions on restart"
    )
    assert row["platform"] == "polymarket"
    assert float(row["amount_usdc"]) == pytest.approx(orphan_size)

    conn.close()


# ── Scenario B: DB corruption at startup ─────────────────────────────────────

def test_startup_check_exits_on_corrupted_db(tmp_path):
    """startup_check must call sys.exit(1) when PRAGMA integrity_check fails.

    Spawns a subprocess that writes garbage into a DB then calls run_all.
    """
    db_path = tmp_path / "corrupt.db"
    # Create a valid DB first, then corrupt it
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.close()
    raw = db_path.read_bytes()
    corrupted = b"\xff" * 100 + raw[100:]
    db_path.write_bytes(corrupted)

    script = textwrap.dedent(f"""
        import asyncio
        import sys
        sys.path.insert(0, "{Path(__file__).parent.parent}")
        import startup_check
        config = {{
            "db_path": "{db_path}",
            "dry_run": True,
        }}
        asyncio.run(startup_check.run_all(config))
    """)

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=15
    )
    assert proc.returncode == 1, (
        f"startup_check must exit(1) on corrupt DB. returncode={proc.returncode}\n"
        f"stderr: {proc.stderr[:500]}"
    )
    assert "integrity" in proc.stderr.lower() or "integrity" in proc.stdout.lower(), (
        "Exit message must mention 'integrity'"
    )


# ── Scenario C: Rust bridge death ─────────────────────────────────────────────

def test_python_exits_when_rust_subprocess_dies(tmp_path):
    """Python main process must exit within 5s when its Rust child is killed.

    Uses a minimal script that mimics main.py's Rust-watching logic:
    spawns a child, monitors it, and exits when the child dies.
    """
    watcher_script = tmp_path / "watcher.py"
    watcher_script.write_text(textwrap.dedent("""
        import subprocess
        import sys
        import time

        # Start a child process that runs for 30s (stands in for Rust)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        pid_file = sys.argv[1]
        with open(pid_file, "w") as f:
            f.write(str(child.pid))

        # Monitor loop — same pattern as main.py
        start = time.monotonic()
        while True:
            ret = child.poll()
            if ret is not None:
                print(f"[rust] process exited with code {ret}", flush=True)
                sys.exit(1)
            if time.monotonic() - start > 15:
                # Safety valve — test should kill child before this
                sys.exit(2)
            time.sleep(0.1)
    """))

    pid_file = tmp_path / "child.pid"
    proc = subprocess.Popen(
        [sys.executable, str(watcher_script), str(pid_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )

    # Wait for child PID to appear
    deadline = time.monotonic() + 5.0
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_file.exists(), "Child PID file must be written"
    child_pid = int(pid_file.read_text().strip())

    # Kill the "Rust" subprocess
    try:
        import os
        os.kill(child_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    # Python watcher must exit within 5s
    try:
        stdout, stderr = proc.communicate(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("Python main process did not exit within 5s after Rust child was killed")

    assert proc.returncode == 1, f"Watcher must exit with code 1, got {proc.returncode}"
    assert "[rust] process exited" in stdout, (
        f"Must log '[rust] process exited'. stdout={stdout!r}"
    )
