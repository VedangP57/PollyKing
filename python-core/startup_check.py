import asyncio
import logging
import sqlite3
import sys

import aiohttp

log = logging.getLogger(__name__)

_POLYMARKET_PING_URL = "https://clob.polymarket.com/ok"


async def _ping_url(url: str) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return resp.status == 200
    except Exception as e:
        log.debug("Ping failed for %s: %s", url, e)
        return False


def _check_db_integrity(db_path: str) -> bool:
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result is not None and result[0] == "ok"
    except Exception as e:
        log.error("DB integrity check failed: %s", e)
        return False


async def run_all(config: dict) -> None:
    """Run all pre-flight checks. Raises SystemExit(1) if any check fails."""
    dry_run = config.get("dry_run", True)
    db_path = config.get("db_path", "data/trades.db")

    # Checks 1 & 2: API keys required in live mode
    if not dry_run:
        if not config.get("polymarket_private_key", "").strip():
            log.critical("STARTUP FAIL: POLYMARKET_PRIVATE_KEY is empty")
            sys.exit(1)
        if not config.get("polymarket_wallet_address", "").strip():
            log.critical("STARTUP FAIL: POLYMARKET_WALLET_ADDRESS is empty")
            sys.exit(1)
        if not config.get("kalshi_api_key", "").strip():
            log.critical("STARTUP FAIL: KALSHI_API_KEY is empty")
            sys.exit(1)
        if not config.get("kalshi_api_secret", "").strip():
            log.critical("STARTUP FAIL: KALSHI_API_SECRET is empty")
            sys.exit(1)

    # Check 3: Kalshi public API reachable
    kalshi_url = config.get("kalshi_api_url", "https://api.elections.kalshi.com/trade-api/v2")
    kalshi_ping = kalshi_url.rstrip("/") + "/exchange/status"
    if not await _ping_url(kalshi_ping):
        log.critical("STARTUP FAIL: Kalshi API unreachable at %s — check network/VPN", kalshi_ping)
        sys.exit(1)

    # Check 4: Polymarket CLOB reachable (non-fatal — geo-blocked in some regions)
    if not await _ping_url(_POLYMARKET_PING_URL):
        log.warning("STARTUP WARN: Polymarket CLOB unreachable — cross-platform mode will not work")

    # Check 5: DB integrity
    if not _check_db_integrity(db_path):
        log.critical(
            "STARTUP FAIL: %s failed integrity_check — DB may be corrupt. "
            "Run: sqlite3 %s 'PRAGMA integrity_check'", db_path, db_path
        )
        sys.exit(1)

    log.info("Startup checks passed (dry_run=%s)", dry_run)
