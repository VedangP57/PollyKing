use arb::comparator::compute_gap_cents;

#[test]
fn test_gap_math_basic() {
    // poly NO = 0.29, kalshi YES = 0.58 → combined = 0.87 → gap = 13c
    let poly_no = 0.29;
    let kalshi_yes = 0.58;
    let gap = compute_gap_cents(poly_no, kalshi_yes);
    assert!((gap - 13.0).abs() < 0.1, "Expected ~13c, got {}", gap);
}

#[test]
fn test_gap_zero_when_combined_is_one() {
    let poly_no = 0.42;
    let kalshi_yes = 0.58;
    let gap = compute_gap_cents(poly_no, kalshi_yes);
    assert!(gap.abs() < 0.01, "Expected ~0c gap, got {}", gap);
}

#[test]
fn test_gap_negative_when_overpriced() {
    let poly_no = 0.50;
    let kalshi_yes = 0.60;
    let gap = compute_gap_cents(poly_no, kalshi_yes);
    assert!(gap < 0.0, "Expected negative gap (no arb opportunity), got {}", gap);
}

#[test]
fn test_gap_large() {
    // poly NO = 0.20, kalshi YES = 0.60 → combined = 0.80 → gap = 20c
    let poly_no = 0.20;
    let kalshi_yes = 0.60;
    let gap = compute_gap_cents(poly_no, kalshi_yes);
    assert!((gap - 20.0).abs() < 0.1, "Expected ~20c, got {}", gap);
}

#[test]
fn test_gap_minimum_threshold() {
    let min_gap = 5.0_f64;
    let max_gap = 30.0_f64;

    // 4c gap — below threshold
    let gap_small = compute_gap_cents(0.32, 0.64); // combined 0.96 → 4c
    assert!(gap_small < min_gap || gap_small.abs() < min_gap);

    // 13c gap — within range
    let gap_valid = compute_gap_cents(0.29, 0.58);
    assert!(gap_valid >= min_gap && gap_valid <= max_gap);

    // 35c gap — above max (data error)
    let gap_large = compute_gap_cents(0.15, 0.50); // combined 0.65 → 35c
    assert!(gap_large > max_gap);
}

#[test]
fn test_price_normalization_boundaries() {
    // YES price 0 → NO price 1
    let gap = compute_gap_cents(1.0, 0.0);
    assert!((gap - 0.0).abs() < 0.01);

    // YES price 1 → NO price 0
    let gap2 = compute_gap_cents(0.0, 1.0);
    assert!((gap2 - 0.0).abs() < 0.01);
}
