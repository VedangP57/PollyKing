import os as _os
import sys
import time
from loguru import logger
import metrics as _metrics
import socket as _socket
from pathlib import Path as _Path

_SOCK_PATH = str(_Path(__file__).parent.parent / "data" / "polyking_events.sock")


def _notify(token: str) -> None:
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.connect(_SOCK_PATH)
            s.sendall((token + "\n").encode())
    except OSError:
        pass  # Tauri not running — ignore


# Rate-limit noisy logs — same gap+reason only printed once per cooldown window
_skip_last: dict[str, float] = {}
_gap_last: dict[str, float] = {}
_SKIP_COOLDOWN = 60.0   # seconds between duplicate SKIP lines
_GAP_COOLDOWN = 10.0    # seconds between duplicate GAP lines for same market

logger.remove()
logger.add(
    sys.stdout,
    format="<dim>[{time:HH:mm:ss}]</dim> <level>{level:<5}</level> | {message}",
    colorize=True,
    level="DEBUG",
)
_log_dir = _os.getenv("LOG_DIR", "logs")
_os.makedirs(_log_dir, exist_ok=True)
logger.add(
    f"{_log_dir}/arb_structured.jsonl",
    format="{time:YYYY-MM-DDTHH:mm:ss.SSSZ} {level} {message}",
    serialize=True,
    rotation="100 MB",
    retention="7 days",
    level="INFO",
)


def startup(dry_run: bool, pair_count: int, high: int, medium: int, low: int) -> None:
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"Bot started. Mode={mode}")
    logger.info(
        f"{pair_count} market pairs loaded ({high} high confidence, {medium} medium, {low} low)"
    )


def ws_connected(platform: str) -> None:
    logger.info(f"WebSocket connected: {platform}")


def ws_disconnected(platform: str) -> None:
    logger.warning(f"WebSocket disconnected: {platform}")


def gap_detected(gap: dict) -> None:
    market_id = gap['market_id']
    now = time.monotonic()
    if now - _gap_last.get(market_id, 0) < _GAP_COOLDOWN:
        return  # Same gap still active — don't spam terminal every second
    _gap_last[market_id] = now
    _metrics.inc_gap_detected(
        pair_type=gap.get("pair_type", "cross_platform"),
        confidence=gap.get("confidence", "medium"),
    )
    _metrics.observe_gap_cents(
        pair_type=gap.get("pair_type", "cross_platform"),
        cents=gap.get("gap_cents", 0.0),
    )
    logger.opt(colors=True).info(
        f"<yellow>GAP</yellow>   | {market_id} "
        f"| Poly: {gap['polymarket_price']:.2f} "
        f"| Kalshi: {gap['kalshi_price']:.2f} "
        f"| Gap: {gap['gap_cents']:.1f}c "
        f"| Conf: {gap.get('confidence', 'medium').upper()}"
    )
    _notify("gap")


def gap_rejected(market_id: str, reason: str) -> None:
    key = f"{market_id}:{reason[:40]}"
    now = time.monotonic()
    if now - _skip_last.get(key, 0) < _SKIP_COOLDOWN:
        return  # Suppress duplicate — same market+reason within cooldown window
    _skip_last[key] = now
    _metrics.inc_gap_rejected(
        reason=_metrics._categorize_rejection(reason),
        pair_type="cross_platform",
    )
    logger.opt(colors=True).debug(
        f"<dim>SKIP</dim>  | {market_id} | {reason}"
    )


def gap_valid(market_id: str) -> None:
    logger.opt(colors=True).info(
        f"<green>VALID</green> | {market_id} | Gap stable. Executing..."
    )


def trade_executed(trade: dict) -> None:
    dry_tag = " (DRY RUN)" if trade.get("dry_run") else ""
    logger.opt(colors=True).info(
        f"<cyan>TRADE</cyan> | {trade.get('polymarket_side')} Poly ${trade.get('polymarket_amount', 0):.2f} "
        f"| {trade.get('kalshi_side')} Kalshi ${trade.get('kalshi_amount', 0):.2f} "
        f"| Expected: +${trade.get('expected_profit', 0):.2f}{dry_tag}"
    )
    _notify("trade")


def trade_logged(trade_id: int) -> None:
    logger.opt(colors=True).info(
        f"<dim>LOG</dim>   | Trade #{trade_id} written to trades.db"
    )


def daily_summary(trade_count: int, simulated_profit: float) -> None:
    logger.info(
        f"Daily summary: {trade_count} trades | Simulated profit: ${simulated_profit:.2f}"
    )
