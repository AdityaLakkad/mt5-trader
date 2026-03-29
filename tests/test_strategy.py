"""
tests/test_strategy.py
=======================
Unit tests for BB Engulfing Breakout strategy helper functions.

Run from project root:
    python -m pytest tests/ -v
    python tests/test_strategy.py        (no pytest needed)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import unittest

from strategy.bb_engulfing_breakout import (
    is_engulfing,
    compute_bb,
    candle_touches_lower_bb,
    candle_touches_upper_bb,
    BBEngulfingParams,
    SizingMode,
    TPSLMode,
)


def make_candle(open_, close, high=None, low=None,
                lower_bb=None, upper_bb=None, middle_bb=None) -> pd.Series:
    """Helper: build a candle Series."""
    return pd.Series({
        "open":      open_,
        "close":     close,
        "high":      high      or max(open_, close) + 1,
        "low":       low       or min(open_, close) - 1,
        "lower_bb":  lower_bb  or 0.0,
        "upper_bb":  upper_bb  or 9999.0,
        "middle_bb": middle_bb or (open_ + close) / 2,
    })


# =============================================================================
# is_engulfing tests
# =============================================================================

class TestIsEngulfing(unittest.TestCase):

    def test_bullish_no_tolerance(self):
        """Green candle with body > previous body."""
        current  = make_candle(100, 106)  # body = 6
        previous = make_candle(102, 104)  # body = 2
        self.assertEqual(is_engulfing(current, previous, tolerance_pct=0.0), "bullish")

    def test_bearish_no_tolerance(self):
        """Red candle with body > previous body."""
        current  = make_candle(106, 100)  # body = 6
        previous = make_candle(104, 102)  # body = 2
        self.assertEqual(is_engulfing(current, previous, tolerance_pct=0.0), "bearish")

    def test_not_engulfing_smaller_body(self):
        """Current body smaller than previous — not engulfing."""
        current  = make_candle(100, 101)  # body = 1
        previous = make_candle(100, 103)  # body = 3
        self.assertIsNone(is_engulfing(current, previous, tolerance_pct=0.0))

    def test_tolerance_expansion_makes_it_pass(self):
        """
        Current body = 5, previous body = 5.5
        At 10% tolerance: expanded = 5 + (0.5 * 2) = 6 > 5.5 → passes
        At 0%  tolerance: 5 > 5.5 → fails
        """
        current  = make_candle(100, 105)  # body = 5, bullish
        previous = make_candle(100, 105.5)  # body = 5.5
        self.assertIsNone(is_engulfing(current, previous, tolerance_pct=0.0))
        self.assertEqual(is_engulfing(current, previous, tolerance_pct=10.0), "bullish")

    def test_doji_current_returns_none(self):
        """Doji (open == close) is not engulfing."""
        current  = make_candle(100, 100)
        previous = make_candle(100, 102)
        self.assertIsNone(is_engulfing(current, previous, tolerance_pct=0.0))

    def test_doji_previous_returns_none(self):
        """Previous body = 0 (doji) — skip to avoid division edge cases."""
        current  = make_candle(100, 105)
        previous = make_candle(102, 102)  # body = 0
        self.assertIsNone(is_engulfing(current, previous, tolerance_pct=0.0))

    def test_exactly_equal_body_no_tolerance(self):
        """Current body == previous body — NOT engulfing (must be strictly greater)."""
        current  = make_candle(100, 105)  # body = 5
        previous = make_candle(100, 105)  # body = 5
        self.assertIsNone(is_engulfing(current, previous, tolerance_pct=0.0))

    def test_exactly_equal_body_with_tolerance(self):
        """Equal bodies but tolerance makes expanded > prev → engulfing."""
        current  = make_candle(100, 105)  # body = 5
        previous = make_candle(100, 105)  # body = 5
        # expanded = 5 + (5*0.10*2) = 6 > 5 → passes
        self.assertEqual(is_engulfing(current, previous, tolerance_pct=10.0), "bullish")

    def test_high_tolerance_always_passes(self):
        """Very high tolerance — almost any candle should pass."""
        current  = make_candle(100, 101)  # body = 1
        previous = make_candle(100, 110)  # body = 10
        # expanded = 1 + (1*2.0*2) = 5 — still < 10 at 200%
        self.assertIsNone(is_engulfing(current, previous, tolerance_pct=200.0))
        # With 500%: expanded = 1 + (1*5.0*2) = 11 > 10 → passes
        self.assertEqual(is_engulfing(current, previous, tolerance_pct=500.0), "bullish")


# =============================================================================
# Bollinger Band tests
# =============================================================================

class TestComputeBB(unittest.TestCase):

    def setUp(self):
        """Create a simple DataFrame with 50 bars."""
        import numpy as np
        prices = [100 + i * 0.1 + (i % 5) * 0.2 for i in range(50)]
        self.df = pd.DataFrame({
            "open":   [p - 0.1 for p in prices],
            "high":   [p + 0.3 for p in prices],
            "low":    [p - 0.3 for p in prices],
            "close":  prices,
            "volume": [1000] * 50,
        })

    def test_columns_added(self):
        result = compute_bb(self.df, period=20, std_dev=2.0)
        self.assertIn("upper_bb",  result.columns)
        self.assertIn("lower_bb",  result.columns)
        self.assertIn("middle_bb", result.columns)

    def test_does_not_mutate_original(self):
        original_cols = list(self.df.columns)
        compute_bb(self.df, period=20, std_dev=2.0)
        self.assertEqual(list(self.df.columns), original_cols)

    def test_upper_above_lower(self):
        result = compute_bb(self.df, period=20, std_dev=2.0).dropna()
        self.assertTrue((result["upper_bb"] > result["lower_bb"]).all())

    def test_middle_between_bands(self):
        result = compute_bb(self.df, period=20, std_dev=2.0).dropna()
        self.assertTrue((result["middle_bb"] < result["upper_bb"]).all())
        self.assertTrue((result["middle_bb"] > result["lower_bb"]).all())

    def test_nan_for_first_period_rows(self):
        result = compute_bb(self.df, period=20, std_dev=2.0)
        # First 19 rows should be NaN
        self.assertTrue(result["upper_bb"].iloc[:19].isna().all())
        # Row 20 onwards should be valid
        self.assertFalse(result["upper_bb"].iloc[20:].isna().any())


# =============================================================================
# BB touch tests
# =============================================================================

class TestBBTouch(unittest.TestCase):

    def test_touches_lower_bb_valid(self):
        """Open below lower, close above lower → long setup."""
        candle = make_candle(open_=99.0, close=101.0, lower_bb=100.0)
        self.assertTrue(candle_touches_lower_bb(candle))

    def test_touches_lower_bb_invalid_open_above(self):
        """Open above lower → not a valid setup."""
        candle = make_candle(open_=101.0, close=103.0, lower_bb=100.0)
        self.assertFalse(candle_touches_lower_bb(candle))

    def test_touches_lower_bb_invalid_close_below(self):
        """Close below lower → didn't push through."""
        candle = make_candle(open_=99.0, close=99.5, lower_bb=100.0)
        self.assertFalse(candle_touches_lower_bb(candle))

    def test_touches_upper_bb_valid(self):
        """Open above upper, close below upper → short setup."""
        candle = make_candle(open_=101.0, close=99.0, upper_bb=100.0)
        self.assertTrue(candle_touches_upper_bb(candle))

    def test_touches_upper_bb_invalid_open_below(self):
        """Open below upper → not a valid setup."""
        candle = make_candle(open_=99.0, close=97.0, upper_bb=100.0)
        self.assertFalse(candle_touches_upper_bb(candle))

    def test_touches_upper_bb_invalid_close_above(self):
        """Close above upper → didn't push through."""
        candle = make_candle(open_=101.0, close=101.5, upper_bb=100.0)
        self.assertFalse(candle_touches_upper_bb(candle))


# =============================================================================
# BBEngulfingParams tests
# =============================================================================

class TestBBEngulfingParams(unittest.TestCase):

    def test_default_params(self):
        p = BBEngulfingParams()
        self.assertEqual(p.bb_period, 20)
        self.assertEqual(p.bb_std_dev, 2.0)
        self.assertEqual(p.engulf_tolerance_pct, 10.0)
        self.assertEqual(p.expiry_candles, 5)
        self.assertEqual(p.sizing_mode, SizingMode.FIXED_USD)
        self.assertEqual(p.tpsl_mode, TPSLMode.POINTS)

    def test_custom_params(self):
        p = BBEngulfingParams(
            bb_period=10,
            sizing_mode=SizingMode.RISK_PCT,
            risk_pct=2.0,
            tpsl_mode=TPSLMode.PERCENT,
            tp_pct=3.0,
        )
        self.assertEqual(p.bb_period, 10)
        self.assertEqual(p.sizing_mode, SizingMode.RISK_PCT)
        self.assertEqual(p.risk_pct, 2.0)
        self.assertEqual(p.tp_pct, 3.0)


# =============================================================================
# Run tests
# =============================================================================

def run_tests():
    """Run all tests and print results without pytest."""
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestIsEngulfing))
    suite.addTests(loader.loadTestsFromTestCase(TestComputeBB))
    suite.addTests(loader.loadTestsFromTestCase(TestBBTouch))
    suite.addTests(loader.loadTestsFromTestCase(TestBBEngulfingParams))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    if result.wasSuccessful():
        print(f"\n✅ All {result.testsRun} tests passed.")
    else:
        print(f"\n❌ {len(result.failures)} failures, {len(result.errors)} errors.")
        return False
    return True


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
