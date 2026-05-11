use arb::comparator::compute_gap_cents;
use arb::types::Gap;

#[test]
fn test_gap_math_basic() {
    // combined = 0.45 + 0.47 = 0.92 → 8c gap
    assert!((compute_gap_cents(0.45, 0.47) - 8.0).abs() < 0.001);
}

#[test]
fn test_gap_zero_when_combined_is_one() {
    assert!(compute_gap_cents(0.5, 0.5).abs() < 0.001);
}

#[test]
fn test_gap_negative_when_overpriced() {
    assert!(compute_gap_cents(0.6, 0.5) < 0.0);
}

#[test]
fn test_gap_large() {
    // combined = 0.1 + 0.2 = 0.3 → 70c gap
    assert!((compute_gap_cents(0.1, 0.2) - 70.0).abs() < 0.001);
}

#[test]
fn test_gap_minimum_threshold() {
    // 0.48 + 0.47 = 0.95 → 5c gap (at minimum)
    assert!((compute_gap_cents(0.48, 0.47) - 5.0).abs() < 0.001);
}

#[test]
fn test_price_normalization_boundaries() {
    // Prices at extremes should not panic
    assert!(compute_gap_cents(0.0, 0.0).is_finite());
    assert!(compute_gap_cents(1.0, 1.0).is_finite());
}

#[test]
fn test_gap_has_kalshi_action_field() {
    // Gap struct must carry kalshi_action for Python executor
    let g = Gap::new(
        "cross_platform".into(),
        "mkt".into(),
        0.70, 0.22,
        "no_token_hex".into(),
        "KXTEST".into(),
        "buy".into(),
        8.0,
        50.0,
        30.0,
    );
    assert_eq!(g.kalshi_action, "buy");
    assert_eq!(g.polymarket_token, "no_token_hex");
    assert!((g.polymarket_price - 0.70).abs() < 0.001);
}

#[test]
fn test_direction2_gap_uses_sell_action() {
    let g = Gap::new(
        "cross_platform".into(),
        "mkt-rev".into(),
        0.28, 0.65,
        "yes_token_hex".into(),
        "KXTEST".into(),
        "sell".into(),
        7.0,
        40.0,
        60.0,
    );
    assert_eq!(g.kalshi_action, "sell");
    assert_eq!(g.polymarket_token, "yes_token_hex");
}
