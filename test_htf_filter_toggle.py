"""
Tests for the higher-timeframe filter toggle (DISABLE_HTF_FILTER).

The 4H HTF veto normally drops longs in a bearish 4H trend and shorts in a
bullish one. DISABLE_HTF_FILTER is a backtest-research knob that turns that veto
off so a filter-on vs filter-off backtest can measure whether the filter earns
its keep:
  * default off  => the veto runs (byte-identical to live behaviour),
  * on           => HTF-disagreeing trades are kept (the veto never fires),
  * it only affects the HTF gate — score/ADX/persistence/session gates are
    untouched, so a trade that disagrees on HTF but fails another gate still
    sits out.

DISABLE_HTF_FILTER must never be set on a live workflow; these tests only
exercise the pure decision path.

Run with:  python -m unittest test_htf_filter_toggle
"""

import unittest
from unittest import mock

import eth_report_bot as bot


# adx high and previous_raw_direction supplied so ADX and persistence pass;
# session=None so the session gate is off. htf_trend is set per-test.
_LONG_KW = dict(price=100.0, atr_value=1.0, supports=[95.0], resistances=[110.0],
                adx_value=30.0, previous_raw_direction="long", session=None)
_SHORT_KW = dict(price=100.0, atr_value=1.0, supports=[90.0], resistances=[105.0],
                 adx_value=30.0, previous_raw_direction="short", session=None)


def _plan(score, disable, **kw):
    with mock.patch.object(bot, "DISABLE_HTF_FILTER", disable):
        return bot.suggest_trade_plan(score=score, **kw)


class FilterOnVetoes(unittest.TestCase):
    """Default (filter on): HTF-disagreeing trades are vetoed."""

    def test_long_vetoed_by_bearish_htf(self):
        plan = _plan(80, False, htf_trend="bearish", **_LONG_KW)
        self.assertIsNone(plan["direction"])
        self.assertEqual(plan["raw_direction"], "long")

    def test_short_vetoed_by_bullish_htf(self):
        plan = _plan(20, False, htf_trend="bullish", **_SHORT_KW)
        self.assertIsNone(plan["direction"])
        self.assertEqual(plan["raw_direction"], "short")

    def test_agreeing_trade_passes(self):
        # Long with bullish HTF is never vetoed regardless of the knob.
        plan = _plan(80, False, htf_trend="bullish", **_LONG_KW)
        self.assertEqual(plan["direction"], "long")


class FilterOffKeepsTrade(unittest.TestCase):
    """Disabled: HTF-disagreeing trades are kept."""

    def test_long_kept_against_bearish_htf(self):
        plan = _plan(80, True, htf_trend="bearish", **_LONG_KW)
        self.assertEqual(plan["direction"], "long")

    def test_short_kept_against_bullish_htf(self):
        plan = _plan(20, True, htf_trend="bullish", **_SHORT_KW)
        self.assertEqual(plan["direction"], "short")


class DisableTouchesOnlyHtfGate(unittest.TestCase):
    """Turning the HTF veto off must not bypass the other gates."""

    def test_adx_still_rejects_when_htf_disabled(self):
        plan = _plan(80, True, htf_trend="bearish",
                     **{**_LONG_KW, "adx_value": 5.0})
        self.assertIsNone(plan["direction"])

    def test_neutral_score_still_sits_out_when_htf_disabled(self):
        plan = _plan(55, True, htf_trend="bearish", **_LONG_KW)
        self.assertIsNone(plan["direction"])
        self.assertIsNone(plan["raw_direction"])

    def test_persistence_still_rejects_when_htf_disabled(self):
        plan = _plan(80, True, htf_trend="bearish",
                     **{**_LONG_KW, "previous_raw_direction": "short"})
        self.assertIsNone(plan["direction"])


class DefaultOffMatchesLive(unittest.TestCase):
    """With the knob at its default, behaviour is identical to the veto running."""

    def test_module_default_is_off(self):
        self.assertFalse(bot.DISABLE_HTF_FILTER)


if __name__ == "__main__":
    unittest.main()
