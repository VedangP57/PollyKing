# Execution success probability by confidence tier
_P_EXEC = {"high": 0.92, "medium": 0.85, "low": 0.75}


def compute_kelly_size(
    bankroll: float,
    price: float,
    p_win: float,
    fraction: float = 0.25,
    max_bet_pct: float = 0.05,
    min_bet_usdc: float = 10.0,
    max_bet_usdc: float = 100.0,
) -> dict:
    """Fractional Kelly sizing for binary contracts.

    price: cost per $1-payout contract (0–1), treated as combined stake for arb
    p_win: estimated win probability
    fraction: Kelly multiplier (0.1–0.25 typical)
    max_bet_pct: hard cap as fraction of bankroll
    Returns: {"action": "BET"|"NO_BET", "bet_usdc": float, "f_star": float, "f": float}
    """
    if price <= 0 or price >= 1:
        return {"action": "NO_BET", "reason": "invalid price",
                "bet_usdc": 0.0, "f_star": 0.0, "f": 0.0}

    b = (1.0 - price) / price
    q = 1.0 - p_win
    f_star = (b * p_win - q) / b

    if f_star <= 0:
        return {"action": "NO_BET", "reason": "negative edge",
                "bet_usdc": 0.0, "f_star": round(f_star, 6), "f": 0.0}

    f = min(f_star * fraction, max_bet_pct)
    bet_raw = bankroll * f
    bet = min(max(bet_raw, min_bet_usdc), max_bet_usdc)

    return {
        "action": "BET",
        "bet_usdc": round(bet, 2),
        "f_star": round(f_star, 6),
        "f": round(f, 6),
    }


def compute_arb_kelly_size(
    bankroll: float,
    combined: float,
    confidence: str,
    fraction: float = 0.25,
    max_bet_pct: float = 0.05,
    min_bet_usdc: float = 10.0,
    max_bet_usdc: float = 100.0,
) -> dict:
    """Kelly sizing for two-leg arbitrage positions.

    combined: sum of both leg prices (e.g. 0.92 for 8¢ gap)
    confidence: "high" | "medium" | "low" → maps to p_exec
    """
    p_exec = _P_EXEC.get(confidence, 0.85)
    return compute_kelly_size(
        bankroll=bankroll,
        price=combined,
        p_win=p_exec,
        fraction=fraction,
        max_bet_pct=max_bet_pct,
        min_bet_usdc=min_bet_usdc,
        max_bet_usdc=max_bet_usdc,
    )
