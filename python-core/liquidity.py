def estimate_slippage_cents(bet_usdc: float, top_of_book_usdc: float,
                            base_slippage: float = 0.3, impact_factor: float = 5.0) -> float:
    if top_of_book_usdc <= 0:
        return base_slippage + impact_factor  # no depth — max slippage
    depth_ratio = min(bet_usdc / top_of_book_usdc, 1.0)
    return base_slippage + (depth_ratio ** 1.5) * impact_factor
