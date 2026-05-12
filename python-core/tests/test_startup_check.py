import pytest
import sqlite3
import tempfile
import os
from unittest.mock import AsyncMock, patch
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import startup_check


@pytest.fixture
def good_db(tmp_path):
    db_path = str(tmp_path / "trades.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def bad_db(tmp_path):
    db_path = str(tmp_path / "corrupt.db")
    with open(db_path, "wb") as f:
        f.write(b"not a sqlite database " * 10)
    return db_path


@pytest.mark.asyncio
async def test_good_db_passes(good_db):
    """A valid SQLite DB passes the integrity check."""
    config = {"dry_run": True, "db_path": good_db}
    with patch("startup_check._ping_url", new=AsyncMock(return_value=True)):
        await startup_check.run_all(config)  # should not raise


@pytest.mark.asyncio
async def test_bad_db_raises_system_exit(bad_db):
    """A corrupted DB triggers SystemExit(1)."""
    config = {"dry_run": True, "db_path": bad_db}
    with patch("startup_check._ping_url", new=AsyncMock(return_value=True)):
        with pytest.raises(SystemExit) as exc_info:
            await startup_check.run_all(config)
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_missing_api_keys_raises_in_live_mode(good_db):
    """Empty POLYMARKET_PRIVATE_KEY → SystemExit(1) when not dry_run."""
    config = {
        "dry_run": False,
        "db_path": good_db,
        "polymarket_private_key": "",
        "polymarket_wallet_address": "0xabc",
        "kalshi_api_key": "key",
        "kalshi_api_secret": "secret",
    }
    with patch("startup_check._ping_url", new=AsyncMock(return_value=True)):
        with pytest.raises(SystemExit) as exc_info:
            await startup_check.run_all(config)
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_kalshi_unreachable_raises(good_db):
    """Kalshi ping failure → SystemExit(1)."""
    config = {"dry_run": True, "db_path": good_db}

    async def fake_ping(url):
        if "kalshi" in url:
            return False
        return True

    with patch("startup_check._ping_url", side_effect=fake_ping):
        with pytest.raises(SystemExit) as exc_info:
            await startup_check.run_all(config)
    assert exc_info.value.code == 1
