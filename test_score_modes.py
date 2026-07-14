"""
Tests for the bias-score model dispatch and the regime-aware score.

Two things are pinned here:
  1. SCORE_MODE dispatch — the default "momentum" path is byte-identical to
     the original scorer (the golden 19/75/5 characterization in test_scoring
     still guards the exact values), and "regime" selects the new model.
  2. The regime score does the one thing it exists to do: in a range it fades
     stretch/RSI extremes (mean-reversion), and in a trend it follows
     momentum. ADX is monkeypatched so the regime is set deterministically
     and the blend can be checked in isolation.

All deterministic, no network, no RNG.

Run with:  python -m unittest test_score_modes
"""

import unittest

import eth_report_bot as bot


def flat_candles(n=80, price=100.0, hl=1.0, vol=1000.0):
    """A flat price series (so EMA anchor ~= price and ADX would be low),
    with a little high/low range so ATR is nonzero. The caller nudges the
    final close to create a stretch above/below the anchor."""
    ts = 1_700_000_000_000
    candles = []
    for i in range(n):
        candles.append({
            "ts": ts + i * 3600_000,
            "open": price,
            "high": price + hl,
            "low": price - hl,
            "close": price,
            "vol": vol,
        })
    return candles


def trend_candles(n=80, base=100.0, step=1.0, hl=1.0, vol=1000.0):
    """A steadily rising (step>0) or falling (step<0) series."""
    ts = 1_700_000_000_000
    candles = []
    for i in range(n):
        close = base + step * i
        candles.append({
            "ts": ts + i * 3600_000,
            "open": close,
            "high": close + hl,
            "low": close - hl,
            "close": close,
            "vol": vol,
        })
    return candles


class ScoreModeDispatch(unittest.TestCase):
    def setUp(self):
        self._mode = bot.SCORE_MODE
        self.addCleanup(lambda: setattr(bot, "SCORE_MODE", self._mode))

    def test_default_mode_is_momentum(self):
        self.assertEqual(bot.SCORE_MODE, "momentum")

    def test_momentum_mode_matches_momentum_scorer(self):
        bot.SCORE_MODE = "momentum"
        candles = trend_candles(step=1.0)
        self.assertEqual(bot.compute_bias_score(candles), bot._score_momentum(candles))

    def test_regime_mode_dispatches_to_regime_scorer(self):
        bot.SCORE_MODE = "regime"
        candles = trend_candles(step=1.0)
        self.assertEqual(bot.compute_bias_score(candles), bot._score_regime(candles))


class RegimeScoreFadesInARange(unittest.TestCase):
    """With ADX forced into the range regime (t=0), the score should lean
    against the stretch: price extended above the anchor -> short lean, below
    -> long lean. This is the exact inversion the momentum-only score got
    backwards."""

    def setUp(self):
        self._adx = bot.adx
        bot.adx = lambda *a, **k: 5.0   # below REGIME_ADX_LOW -> pure mean-reversion
        self.addCleanup(lambda: setattr(bot, "adx", self._adx))

    def test_stretched_above_anchor_leans_short(self):
        candles = flat_candles(price=100.0)
        candles[-1]["close"] = 105.0   # well above the ~100 anchor
        candles[-1]["high"] = 106.0
        self.assertLess(bot._score_regime(candles), 50)

    def test_stretched_below_anchor_leans_long(self):
        candles = flat_candles(price=100.0)
        candles[-1]["close"] = 95.0    # well below the ~100 anchor
        candles[-1]["low"] = 94.0
        self.assertGreater(bot._score_regime(candles), 50)


class RegimeScoreFollowsMomentumInATrend(unittest.TestCase):
    """With ADX forced into the trend regime (t=1), the score should follow
    momentum: up-trend -> long lean, down-trend -> short lean."""

    def setUp(self):
        self._adx = bot.adx
        bot.adx = lambda *a, **k: 50.0   # above REGIME_ADX_HIGH -> pure momentum
        self.addCleanup(lambda: setattr(bot, "adx", self._adx))

    def test_uptrend_leans_long(self):
        self.assertGreater(bot._score_regime(trend_candles(step=1.0)), 50)

    def test_downtrend_leans_short(self):
        self.assertLess(bot._score_regime(trend_candles(step=-1.0)), 50)


class RegimeScoreStaysInBounds(unittest.TestCase):
    def test_score_is_clamped_5_to_95(self):
        for candles in (
            flat_candles(),
            trend_candles(step=1.0),
            trend_candles(step=-1.0),
            trend_candles(step=50.0),      # violently extended
            trend_candles(step=-50.0),
        ):
            s = bot._score_regime(candles)
            self.assertGreaterEqual(s, 5)
            self.assertLessEqual(s, 95)


if __name__ == "__main__":
    unittest.main()
