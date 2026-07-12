"""
Tests for the shared signal-scoring logic.

The live bot (eth_report_bot.build_report) and the backtest
(backtest.evaluate_signal_at) must score every candle identically —
otherwise the backtest stops describing the live bot, silently. These
tests pin the bias score against a fixed candle fixture and assert that
the extracted, shared scorer reproduces exactly what the report prints
and what the backtest uses.

Run with:  python -m unittest test_scoring
"""

import math
import re
import unittest

import eth_report_bot as bot


def make_candles(n=260, trend=0.0, base=2000.0):
    """
    Deterministic candle fixture — no RNG, so scores are reproducible.
    `trend` is the per-bar drift: 0 is sideways, positive trends up.
    260 bars is enough for the backtest's WARMUP_CANDLES gate.
    """
    candles = []
    ts = 1_700_000_000_000
    for i in range(n):
        wiggle = math.sin(i / 7.0) * 15 + math.cos(i / 13.0) * 9
        close = base + trend * i + wiggle
        candles.append({
            "ts": ts + i * 3600_000,
            "open": close - wiggle * 0.1,
            "high": close + 5 + (i % 5),
            "low": close - 5 - (i % 3),
            "close": close,
            "vol": 1000 + (i % 10) * 50,
        })
    return candles


def report_score(candles):
    """The bias score as build_report actually prints it, without network."""
    original = bot.higher_timeframe_trend
    bot.higher_timeframe_trend = lambda *a, **k: None  # keep it offline + deterministic
    try:
        report, _ = bot.build_report(candles, previous_raw_direction=None)
    finally:
        bot.higher_timeframe_trend = original
    return int(re.search(r"Bias score: (\d+)/100", report).group(1))


class BiasScoreCharacterization(unittest.TestCase):
    """Golden values captured from the pre-refactor build_report. If a change
    moves these, it has changed live trading behaviour — make that deliberate."""

    def test_neutral_fixture_score(self):
        self.assertEqual(report_score(make_candles(trend=0.0)), 19)

    def test_uptrend_fixture_score(self):
        self.assertEqual(report_score(make_candles(trend=6.0)), 75)

    def test_downtrend_fixture_score(self):
        self.assertEqual(report_score(make_candles(trend=-6.0)), 5)


class SharedScorerInvariant(unittest.TestCase):
    """compute_bias_score is the single source of truth; the report and the
    backtest must both agree with it."""

    def test_scorer_matches_report(self):
        for trend in (0.0, 6.0, -6.0):
            candles = make_candles(trend=trend)
            self.assertEqual(bot.compute_bias_score(candles), report_score(candles))

    def test_backtest_scores_via_shared_scorer(self):
        import backtest as bt
        for trend in (0.0, 6.0, -6.0):
            candles = make_candles(trend=trend)
            score = bot.compute_bias_score(candles)
            # The backtest turns score into a raw_direction with the same
            # thresholds as the live bot; agreement here proves both consume
            # the same score.
            if score >= bot.LONG_SCORE_MIN:
                expected = "long"
            elif score <= bot.SHORT_SCORE_MAX:
                expected = "short"
            else:
                expected = None
            plan = bt.evaluate_signal_at(candles, len(candles) - 1)
            self.assertIsNotNone(plan)
            self.assertEqual(plan["raw_direction"], expected)


class EvaluatePlanIsTheSharedPlanPath(unittest.TestCase):
    """evaluate_plan is the single 'candles -> plan' path. The hourly report,
    the fill-time re-check, and the backtest must all go through it, so a plan
    can't be derived one way live and another way in the backtest."""

    def test_build_report_plan_comes_from_evaluate_plan(self):
        for trend in (0.0, 6.0, -6.0):
            candles = make_candles(trend=trend)
            original = bot.higher_timeframe_trend
            bot.higher_timeframe_trend = lambda *a, **k: None  # offline + deterministic
            try:
                _, report_plan = bot.build_report(candles, previous_raw_direction=None)
                direct = bot.evaluate_plan(candles, previous_raw_direction=None, htf_trend=None)
            finally:
                bot.higher_timeframe_trend = original
            self.assertEqual(report_plan, direct)

    def test_backtest_signal_matches_evaluate_plan(self):
        import backtest as bt
        for trend in (0.0, 6.0, -6.0):
            candles = make_candles(trend=trend)
            i = len(candles) - 1
            # Reproduce the backtest's own HTF input, then confirm the plan it
            # returns is exactly what the shared evaluate_plan produces.
            htf = bot.htf_trend_from_closes(
                [c["close"] for c in bt.resample_htf(candles[:i + 1])])
            expected = bot.evaluate_plan(candles[:i + 1], None, htf_trend=htf)
            self.assertEqual(bt.evaluate_signal_at(candles, i), expected)


class HtfTrendFromCloses(unittest.TestCase):
    """The higher-timeframe trend classifier, shared by the live HTF fetch and
    the backtest's resampled HTF."""

    def test_rising_series_is_bullish(self):
        self.assertEqual(bot.htf_trend_from_closes([float(i) for i in range(60)]), "bullish")

    def test_falling_series_is_bearish(self):
        self.assertEqual(bot.htf_trend_from_closes([float(i) for i in range(60, 0, -1)]), "bearish")

    def test_too_short_is_none(self):
        self.assertIsNone(bot.htf_trend_from_closes([1.0, 2.0, 3.0]))


if __name__ == "__main__":
    unittest.main()
