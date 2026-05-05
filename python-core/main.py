import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

import notifier
import tracker
from detector import GapDetector
from executor import Executor
from matcher import Matcher
from reconciler import Reconciler
from bayes_engine import BayesEngine
from risk_engine import RiskEngine, KillSwitch

load_dotenv()

# ---------------------------------------------------------------------------
# API key requirements:
#
#   Polymarket:
#     - Price feed + market discovery: fully public, no key needed
#     - Order placement: POLYMARKET_API_KEY + POLYMARKET_PRIVATE_KEY (live only)
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
}

rust_process = None

# Per-market cooldown: market_id → monotonic timestamp of last executed trade
# Prevents a burst of queued gap tasks from all trading the same market consecutively
import time as _time
_last_traded: dict[str, float] = {}
_TRADE_COOLDOWN = 60.0  # seconds before same market can trade again


def handle_sigint(sig, frame):
    if rust_process:
        rust_process.terminate()
    sys.exit(0)


signal.signal(signal.SIGINT, handle_sigint)


async def main():
    global rust_process

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

    rust_process = await asyncio.create_subprocess_exec(
        rust_bin,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "MARKETS_JSON": CONFIG["markets_json"]},
    )

    stdout_queue: asyncio.Queue = asyncio.Queue()

    executor = Executor(CONFIG, rust_process.stdin, stdout_queue)

    reconciler = Reconciler(CONFIG, db_conn)
    asyncio.create_task(reconciler.run_forever())

    asyncio.create_task(_read_stderr(rust_process.stderr))
    asyncio.create_task(_read_stdout(rust_process.stdout, stdout_queue, detector, executor, db_conn, bayes_engine))

    await rust_process.wait()


async def _read_stdout(stdout, stdout_queue: asyncio.Queue, detector, executor, db_conn, bayes_engine: BayesEngine):
    async for line in stdout:
        text = line.decode().strip()
        if not text:
            continue

        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue

        event_type = event.get("event")

        if event_type == "gap_detected":
            # Fire-and-forget: _handle_gap waits on stdout_queue for the Rust
            # confirmation. If we awaited it directly here, _read_stdout would
            # block and never read the confirmation line from Rust → deadlock.
            asyncio.create_task(_handle_gap(event, detector, executor, db_conn, stdout_queue, bayes_engine))
        elif event_type == "order_placed":
            await stdout_queue.put(event)


async def _handle_gap(gap: dict, detector: GapDetector, executor: Executor, db_conn, stdout_queue, bayes_engine: BayesEngine):
    notifier.gap_detected(gap)

    market_id = gap["market_id"]

    # Update Bayesian posterior for this market
    poly_price = gap.get("polymarket_price", 0.5)
    bayes_engine.update(market_id, poly_price, prev_price=None)
    posterior = bayes_engine.get_posterior(market_id)
    gap["p_model"] = posterior

    # Per-market cooldown — prevents burst of queued gap tasks from all
    # trading the same market consecutively within the cooldown window
    now = _time.monotonic()
    if now - _last_traded.get(market_id, 0) < _TRADE_COOLDOWN:
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
        notifier.logger.warning(f"No confirmation from Rust for {market_id} — gap logged but trade skipped")
        return

    pair_type = gap.get("pair_type", "cross_platform")
    poly_side = "YES" if pair_type == "internal" else "NO"
    trade = {
        "gap_id": gap_id,
        "polymarket_order_id": confirmation.get("polymarket_order_id"),
        "kalshi_order_id": confirmation.get("kalshi_order_id"),
        "polymarket_side": poly_side,
        "kalshi_side": "YES",
        "polymarket_amount": confirmation.get("total_spent", 0) / 2,
        "kalshi_amount": confirmation.get("total_spent", 0) / 2,
        "amount_usdc": confirmation.get("total_spent"),
        "expected_profit": confirmation.get("expected_profit"),
        "dry_run": confirmation.get("dry_run", True),
    }

    notifier.trade_executed(trade)
    trade_id = tracker.log_trade(db_conn, trade)
    tracker.mark_gap_executed(db_conn, gap_id)
    notifier.trade_logged(trade_id)


async def _read_stderr(stderr):
    async for line in stderr:
        text = line.decode().strip()
        if text:
            notifier.logger.debug(f"[rust] {text}")


if __name__ == "__main__":
    asyncio.run(main())
