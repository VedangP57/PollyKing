import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

import metrics as _metrics
import notifier
import tracker
from detector import GapDetector
from opportunity_engine import OpportunityEngine
from two_leg_executor import TwoLegExecutor
from matcher import Matcher
from reconciler import Reconciler
from bayes_engine import BayesEngine
from risk_engine import RiskEngine
from startup_audit import audit_orphan_positions
import startup_check
from circuit_breaker import CircuitBreaker

load_dotenv()

# ---------------------------------------------------------------------------
# API key requirements:
#
#   Polymarket:
#     - Price feed + market discovery: fully public, no key needed
#     - Order placement: POLYMARKET_PRIVATE_KEY + POLYMARKET_WALLET_ADDRESS (live only)
#
#   Kalshi:
#     - Market list + price data: PUBLIC — https://api.elections.kalshi.com, NO key needed
#     - Order placement: KALSHI_API_KEY + KALSHI_API_SECRET (live only, DRY_RUN=false)
#
#   Mode is determined by markets.json — if cross_platform pairs exist, CROSS PLATFORM mode.
#   Run scripts/backfill_matches.py to fetch both platforms and populate markets.json.
# ---------------------------------------------------------------------------

CONFIG = {
    "dry_run": os.getenv("DRY_RUN", "true").lower() == "true",
    "min_gap_cents": float(os.getenv("MIN_GAP_CENTS", "5")),
    "max_gap_cents": float(os.getenv("MAX_GAP_CENTS", "30")),
    "min_bet_usdc": float(os.getenv("MIN_BET_USDC", "10")),
    "max_bet_usdc": float(os.getenv("MAX_BET_USDC", "100")),
    "max_daily_loss_usdc": float(os.getenv("MAX_DAILY_LOSS_USDC", "50")),
    "max_open_positions": (
        int(os.getenv("MAX_OPEN_POSITIONS", "20"))
        if os.getenv("DRY_RUN", "true").lower() != "true"
        else 999_999
    ),
    "db_path": os.getenv("DB_PATH", "data/trades.db"),
    "rust_binary": os.getenv("RUST_BINARY", "rust-core/target/release/arb"),
    "markets_json": os.getenv("MARKETS_JSON", "config/markets.json"),
    "ev_min_cents": float(os.getenv("EV_MIN_CENTS", "1.0")),
    "ev_taker_fee_rate": float(os.getenv("EV_TAKER_FEE_RATE", "0.02")),
    "ev_slippage_cents": float(os.getenv("EV_SLIPPAGE_CENTS", "0.5")),
    "bankroll_usdc": float(os.getenv("BANKROLL_USDC", "500.0")),
    "kelly_fraction": float(os.getenv("KELLY_FRACTION", "0.25")),
    "reconcile_interval_s": float(os.getenv("RECONCILE_INTERVAL_S", "300.0")),
    "max_category_exposure_usdc": float(os.getenv("MAX_CATEGORY_EXPOSURE_USDC", "200.0")),
    "cross_platform_min_gap_cents": float(os.getenv("CROSS_PLATFORM_MIN_GAP_CENTS", "10")),
    "internal_min_gap_cents": float(os.getenv("INTERNAL_MIN_GAP_CENTS", "8")),
    "polymarket_private_key": os.getenv("POLYMARKET_PRIVATE_KEY", ""),
    "polymarket_wallet_address": os.getenv("POLYMARKET_WALLET_ADDRESS", ""),
    "polymarket_signature_type": int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
    "kalshi_api_key": os.getenv("KALSHI_API_KEY", ""),
    "kalshi_api_secret": os.getenv("KALSHI_API_SECRET", ""),
    "kalshi_api_url": os.getenv("KALSHI_API_URL", "https://api.elections.kalshi.com/trade-api/v2"),
    "fee_cache_refresh_s": float(os.getenv("FEE_CACHE_REFRESH_INTERVAL_S", "3600")),
}

rust_process = None

# Per-market cooldown: market_id → monotonic timestamp of last executed trade
# Prevents a burst of queued gap tasks from all trading the same market consecutively
import time as _time
_last_traded: dict[str, float] = {}
_TRADE_COOLDOWN = 300.0  # seconds before same market can trade again (secondary guard)

# Per-market previous price — needed by BayesEngine to compute likelihood ratio.
# Without prev_price, BayesEngine.update() returns the prior unchanged (no-op).
# Memory: bounded by number of distinct markets seen (typically <200 in markets.json).
# Concurrency-safe: read+write in _handle_gap_inner are synchronous (no await between).
_prev_prices: dict[str, float] = {}
_circuit_breaker = CircuitBreaker()

# Semaphore: max concurrent live API calls — prevents rate limiting on both exchanges
_GAP_SEMAPHORE: asyncio.Semaphore | None = None
_MAX_CONCURRENT_EXECUTIONS = 3


def handle_sigint(sig, frame):
    if rust_process:
        rust_process.terminate()
    sys.exit(0)


signal.signal(signal.SIGINT, handle_sigint)


async def main():
    global rust_process

    # Run backfill if markets.json is >24 hours old or missing cross-platform pairs
    markets_path = Path(CONFIG["markets_json"])
    needs_backfill = False
    if not markets_path.exists():
        needs_backfill = True
    else:
        mtime = datetime.fromtimestamp(markets_path.stat().st_mtime, tz=timezone.utc)
        if datetime.now(timezone.utc) - mtime > timedelta(hours=24):
            needs_backfill = True
        else:
            try:
                _mdata = json.loads(markets_path.read_text())
                has_cross = any(p.get("pair_type") == "cross_platform" for p in _mdata.get("pairs", []))
                if not has_cross:
                    needs_backfill = True
            except Exception:
                needs_backfill = True

    if needs_backfill:
        notifier.logger.info("markets.json is stale or missing cross-platform pairs — running backfill...")
        result = subprocess.run(
            [sys.executable, "scripts/backfill_matches.py"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        if result.returncode != 0:
            notifier.logger.warning(f"Backfill failed: {result.stderr[:200]}")
        else:
            notifier.logger.info("Backfill complete")

    # Determine mode from the actual pairs in markets.json, not from env key presence
    try:
        _pairs_data = json.loads(Path(CONFIG["markets_json"]).read_text()).get("pairs", [])
        _has_cross = any(p.get("pair_type") == "cross_platform" for p in _pairs_data)
        mode = "CROSS PLATFORM" if _has_cross else "INTERNAL"
    except Exception:
        mode = "INTERNAL"
    notifier.logger.info(f"Running in {mode} mode")

    db_conn = tracker.init_db(CONFIG["db_path"])
    risk_engine = RiskEngine(CONFIG, db_conn)
    bayes_engine = BayesEngine()
    matcher = Matcher(CONFIG["markets_json"])
    detector = GapDetector(CONFIG, db_conn, risk_engine)

    # Load pairs from markets.json — prefer new "pairs" format, fall back to manual_pairs
    try:
        _data = json.loads(Path(CONFIG["markets_json"]).read_text())
        pairs = _data.get("pairs", _data.get("manual_pairs", []))
    except (FileNotFoundError, json.JSONDecodeError):
        pairs = []

    # fee_rate_map: market_id → fee_rate for enriching gap events with per-pair fee
    fee_rate_map: dict[str, float] = {
        p.get("market_id", p.get("token_a", "")): p.get("fee_rate", 0.04)
        for p in pairs
    }

    high = sum(1 for p in pairs if p.get("confidence") == "high")
    medium = sum(1 for p in pairs if p.get("confidence") == "medium")
    low = sum(1 for p in pairs if p.get("confidence") == "low")
    notifier.startup(CONFIG["dry_run"], len(pairs), high, medium, low)

    rust_bin = CONFIG["rust_binary"]
    if not Path(rust_bin).exists():
        notifier.logger.error(
            f"Rust binary not found at {rust_bin}. Run: cd rust-core && cargo build --release"
        )
        sys.exit(1)

    await startup_check.run_all(CONFIG)

    rust_process = await asyncio.create_subprocess_exec(
        rust_bin,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "MARKETS_JSON": CONFIG["markets_json"]},
    )

    global _GAP_SEMAPHORE
    _GAP_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_EXECUTIONS)

    from prometheus_client import start_http_server as _prom_start
    _prom_start(9090)
    notifier.logger.info("Prometheus metrics server started on :9090")

    from health import HealthServer as _HealthServer
    _health_state: dict = {"last_gap_seen": 0.0, "ws_connected": [], "open_positions": 0}
    _health_server = _HealthServer(_health_state, port=8080)
    await _health_server.start()
    notifier.logger.info("Health server started on :8080")

    opp_engine = OpportunityEngine(db_conn)

    async def _evict_stale_opportunities():
        while True:
            await asyncio.sleep(60)
            opp_engine.evict_stale()
    asyncio.create_task(_evict_stale_opportunities())

    stdout_queue: asyncio.Queue = asyncio.Queue()

    executor = TwoLegExecutor(CONFIG, db_conn)

    # Live mode only: detect orphan positions from any previous crash
    if not CONFIG["dry_run"]:
        notifier.logger.info("Auditing exchange positions for orphans from prior runs...")
        await audit_orphan_positions(executor._poly, executor._kalshi, db_conn)

    # Warm per-token fee cache from Polymarket CLOB API
    _poly_tokens = [p.get("token_a", "") for p in _pairs_data if p.get("token_a")]
    if _poly_tokens and not CONFIG.get("dry_run", True):
        try:
            await executor._poly.warm_fee_cache(_poly_tokens)
            CONFIG["_fee_cache"] = dict(executor._poly._fee_cache)
        except Exception as e:
            notifier.logger.warning("Fee cache warm-up failed: %s — continuing with defaults", e)

    reconciler = Reconciler(CONFIG, db_conn)
    asyncio.create_task(reconciler.run_forever())

    async def _update_pnl_metric():
        while True:
            await asyncio.sleep(60)
            try:
                pnl = tracker.get_daily_pnl(db_conn)
                _metrics.set_daily_pnl(pnl)
            except Exception:
                pass
    asyncio.create_task(_update_pnl_metric())

    asyncio.create_task(_read_stderr(rust_process.stderr))
    asyncio.create_task(_read_stdout(rust_process.stdout, stdout_queue, detector, executor, db_conn, bayes_engine, fee_rate_map, _health_state, opp_engine))

    await rust_process.wait()


async def _read_stdout(stdout, stdout_queue: asyncio.Queue, detector, executor, db_conn, bayes_engine: BayesEngine, fee_rate_map: dict, health_state: dict | None = None, opp_engine: OpportunityEngine | None = None):
    async for line in stdout:
        text = line.decode().strip()
        if not text:
            continue

        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue

        event_type = event.get("event")

        if event_type == "ws_reconnect":
            platform = event.get("platform", "unknown")
            _metrics.inc_ws_reconnect(platform)
            notifier.logger.debug(f"WS reconnect: {platform}")
        elif event_type == "ws_staleness":
            _metrics.set_ws_staleness(float(event.get("seconds", 0)))
        elif event_type == "gap_detected":
            # Fire-and-forget: _handle_gap calls TwoLegExecutor directly.
            # We do not block here so _read_stdout can keep draining Rust stdout.
            asyncio.create_task(_handle_gap(event, detector, executor, db_conn, stdout_queue, bayes_engine, fee_rate_map, health_state, opp_engine))


async def _handle_gap(gap: dict, detector: GapDetector, executor: TwoLegExecutor, db_conn, stdout_queue, bayes_engine: BayesEngine, fee_rate_map: dict, health_state: dict | None = None, opp_engine: OpportunityEngine | None = None):
    async with _GAP_SEMAPHORE:
        await _handle_gap_inner(gap, detector, executor, db_conn, stdout_queue, bayes_engine, fee_rate_map, health_state, opp_engine)


async def _handle_gap_inner(gap: dict, detector: GapDetector, executor: TwoLegExecutor, db_conn, stdout_queue, bayes_engine: BayesEngine, fee_rate_map: dict, health_state: dict | None = None, opp_engine: OpportunityEngine | None = None):
    if health_state is not None:
        health_state["last_gap_seen"] = _time.monotonic()
    if opp_engine is not None:
        opp = opp_engine.observe(gap)
        gap["opp_id"] = opp.opp_key if opp else None
    notifier.gap_detected(gap)

    market_id = gap["market_id"]

    # Strip "-rev" suffix before fee lookup — Direction 2 rev gaps share the base pair's fee
    _lookup_id = market_id.removesuffix("-rev")
    gap["fee_rate"] = fee_rate_map.get(_lookup_id, fee_rate_map.get(market_id, 0.04))

    # Update Bayesian posterior for this market.
    # Pass prev_price so the likelihood ratio reflects actual price movement.
    poly_price = gap.get("polymarket_price", 0.5)
    prev_price = _prev_prices.get(market_id)
    bayes_engine.update(market_id, poly_price, prev_price=prev_price)
    _prev_prices[market_id] = poly_price
    posterior = bayes_engine.get_posterior(market_id)
    gap["p_model"] = posterior

    # Primary guard: skip if a position is already open for this market (DB-persisted)
    if tracker.has_open_trade(db_conn, market_id):
        return

    # Secondary guard: in-memory cooldown prevents burst of queued tasks from
    # all firing immediately after a position just closed
    now = _time.monotonic()
    if now - _last_traded.get(market_id, 0) < _TRADE_COOLDOWN:
        return

    if _circuit_breaker.is_open(market_id):
        notifier.logger.warning("circuit_breaker open for %s — skipping", market_id)
        return

    is_valid, reason = detector.validate(gap)
    if not is_valid:
        notifier.gap_rejected(market_id, reason)
        return

    notifier.gap_valid(market_id)

    # Mark as traded immediately (before await) so subsequent queued tasks
    # for this market see the cooldown and skip without executing
    _last_traded[market_id] = _time.monotonic()

    gap_id = tracker.log_gap(db_conn, gap)

    confirmation = await executor.execute(gap)

    if not confirmation:
        notifier.logger.warning(f"Execution failed for {market_id} — gap logged, no trade")
        _circuit_breaker.record_failure(market_id)
        _metrics.inc_execution(
            pair_type=gap.get("pair_type", "cross_platform"),
            dry_run=CONFIG.get("dry_run", True),
            outcome="execution_failed",
        )
        return

    _circuit_breaker.reset(market_id)
    _metrics.inc_execution(
        pair_type=gap.get("pair_type", "cross_platform"),
        dry_run=CONFIG.get("dry_run", True),
        outcome="success",
    )

    pair_type = gap.get("pair_type", "cross_platform")
    poly_side = "YES" if pair_type == "internal" else "NO"
    kalshi_side = "YES" if gap.get("kalshi_action", "buy") == "buy" else "NO"
    trade = {
        "gap_id": gap_id,
        "polymarket_order_id": confirmation.get("polymarket_order_id"),
        "kalshi_order_id": confirmation.get("kalshi_order_id"),
        "polymarket_side": poly_side,
        "kalshi_side": kalshi_side,
        "polymarket_amount": confirmation.get("total_spent", 0) / 2,
        "kalshi_amount": confirmation.get("total_spent", 0) / 2,
        "amount_usdc": confirmation.get("total_spent"),
        "gap_cents": confirmation.get("gap_cents"),
        "expected_profit": confirmation.get("expected_profit"),
        "dry_run": CONFIG.get("dry_run", True),
    }

    notifier.trade_executed(trade)
    trade_id = tracker.log_trade(db_conn, trade)
    _attempt_id = confirmation.get("_attempt_id")
    if _attempt_id:
        try:
            tracker.confirm_trade_attempt(db_conn, _attempt_id, trade_id)
        except Exception as _e:
            notifier.logger.warning("confirm_trade_attempt failed (%s)", _e)
    tracker.mark_gap_executed(db_conn, gap_id)
    notifier.trade_logged(trade_id)
    if opp_engine is not None:
        opp_engine.mark_executed(market_id, gap.get("pair_type", "cross_platform"), trade_id)


async def _read_stderr(stderr):
    async for line in stderr:
        text = line.decode().strip()
        if text:
            notifier.logger.debug(f"[rust] {text}")


if __name__ == "__main__":
    asyncio.run(main())
