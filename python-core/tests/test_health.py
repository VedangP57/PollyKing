import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import asyncio


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok():
    from health import HealthServer
    import aiohttp

    state = {"last_gap_seen": 0.0, "ws_connected": ["polymarket", "kalshi"]}
    server = HealthServer(state, port=18080)
    await server.start()

    async with aiohttp.ClientSession() as session:
        async with session.get("http://localhost:18080/health") as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert "last_gap_age_s" in data
            assert "ws_connected" in data

    await server.stop()


@pytest.mark.asyncio
async def test_health_endpoint_stale_feed():
    from health import HealthServer
    import aiohttp
    import time

    state = {"last_gap_seen": time.monotonic() - 200.0, "ws_connected": []}
    server = HealthServer(state, port=18081)
    await server.start()

    async with aiohttp.ClientSession() as session:
        async with session.get("http://localhost:18081/health") as resp:
            data = await resp.json()
            assert data["status"] in ("degraded", "ok")
            assert data["last_gap_age_s"] > 190

    await server.stop()
