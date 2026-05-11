import time
from aiohttp import web


class HealthServer:
    def __init__(self, state: dict, port: int = 8080):
        self._state = state
        self._port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _handle_health(self, request: web.Request) -> web.Response:
        now = time.monotonic()
        last_gap = self._state.get("last_gap_seen", 0.0)
        age_s = now - last_gap if last_gap > 0 else float("inf")
        ws_connected = self._state.get("ws_connected", [])
        open_positions = self._state.get("open_positions", 0)

        status = "ok"
        # Only degrade if we've actually seen at least one gap and it's been >120s.
        # last_gap_seen==0.0 means startup — no gaps expected yet.
        if last_gap > 0 and age_s > 120:
            status = "degraded"

        return web.json_response({
            "status": status,
            "last_gap_age_s": round(age_s, 1) if age_s != float("inf") else None,
            "ws_connected": ws_connected,
            "open_positions": open_positions,
        })
