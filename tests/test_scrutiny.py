"""Tests for scrutiny filters."""

from worklist.scrutiny import ScrutinyConfig, SymbolData, evaluate


def _good_symbol(**overrides) -> SymbolData:
    """Create a symbol that passes all default scrutiny filters."""
    defaults = dict(
        symbol="TEST",
        price=5.00,
        volume=1_000_000,
        rvol=5.0,
        spread_pct=0.5,
        gap_pct=50.0,
        float_millions=10.0,
        dollar_volume=500_000,
        scanner_score=70.0,
    )
    defaults.update(overrides)
    return SymbolData(**defaults)


def test_good_symbol_passes():
    result = evaluate(_good_symbol())
    assert result.passed
    assert len(result.checks_passed) == 8


def test_price_too_low():
    result = evaluate(_good_symbol(price=1.50))
    assert not result.passed
    assert "min_price" in result.checks_failed


def test_price_too_high():
    result = evaluate(_good_symbol(price=25.00))
    assert not result.passed
    assert "max_price" in result.checks_failed


def test_volume_too_low():
    result = evaluate(_good_symbol(volume=100_000))
    assert not result.passed
    assert "volume" in result.checks_failed


def test_rvol_too_low():
    result = evaluate(_good_symbol(rvol=1.5))
    assert not result.passed
    assert "rvol" in result.checks_failed


def test_spread_too_wide():
    result = evaluate(_good_symbol(spread_pct=2.0))
    assert not result.passed
    assert "spread" in result.checks_failed


def test_gap_too_small():
    result = evaluate(_good_symbol(gap_pct=10.0))
    assert not result.passed
    assert "gap" in result.checks_failed


def test_float_too_large():
    result = evaluate(_good_symbol(float_millions=50.0))
    assert not result.passed
    assert "float" in result.checks_failed


def test_float_zero_passes():
    """Float=0 means unknown, should pass."""
    result = evaluate(_good_symbol(float_millions=0))
    assert result.passed


def test_dollar_volume_too_low():
    result = evaluate(_good_symbol(dollar_volume=50_000))
    assert not result.passed
    assert "dollar_volume" in result.checks_failed


def test_scanner_score_too_low():
    result = evaluate(_good_symbol(scanner_score=30.0))
    assert not result.passed
    assert "scanner_score" in result.checks_failed


def test_custom_config():
    config = ScrutinyConfig(min_price=1.0, max_price=50.0, min_volume=100, min_rvol=1.0,
                            max_spread_pct=5.0, min_scanner_score=10.0, min_dollar_volume=100,
                            max_float_millions=100.0, min_gap_pct=5.0)
    result = evaluate(_good_symbol(price=1.50, gap_pct=6.0), config)
    assert result.passed
