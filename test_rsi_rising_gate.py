"""
Tests for the REQUIRE_RSI_RISING momentum-slope gate (eth_report_bot).

The bias score already uses the RSI *level*; this optional gate adds a slope
condition — only take a long when RSI rose vs the prior bar, only take a short
when it fell. It's a native-direction gate (evaluated on the score's raw
direction, before any INVERT flip) and, like the other signal gates, must never
change behaviour when off (default), which is what keeps live byte-identical.

Run with:  python -m unittest test_rsi_rising_gate
"""

import unittest
from unittest import mock

import eth_report_bot as bot

# Score clearly long / short; ATR + levels chosen so a plan clears MIN_RR when the
# gate lets the trade through. adx high and previous_raw_direction supplied so the
# ADX and persistence gates pass; htf_trend agrees; session off.
_LONG_KW = dict(price=100.0, atr_value=1.0, supports=[95.0], resistances=[110.0],
                htf_trend="bullish", adx_value=30.0, previous_raw_direction="long", session=None)
_SHORT_KW = dict(price=100.0, atr_value=1.0, supports=[90.0], resistances=[105.0],
                 htf_trend="bearish", adx_value=30.0, previous_raw_direction="short", session=None)


def _plan(score, require, rsi_delta, kw):
    with mock.patch.object(bot, "REQUIRE_RSI_RISING", require):
        return bot.suggest_trade_plan(score=score, rsi_delta=rsi_delta, **kw)


class GateOff(unittest.TestCase):
    """Default: the gate never fires, regardless of RSI slope."""

    def test_long_taken_even_with_falling_rsi(self):
        plan = _plan(80, False, -5.0, _LONG_KW)
        self.assertEqual(plan["direction"], "long")

    def test_short_taken_even_with_rising_rsi(self):
        plan = _plan(20, False, +5.0, _SHORT_KW)
        self.assertEqual(plan["direction"], "short")

    def test_module_default_is_off(self):
        self.assertFalse(bot.REQUIRE_RSI_RISING)


class GateOn(unittest.TestCase):
    """Enabled: momentum must move the trade's way."""

    def test_long_kept_when_rsi_rising(self):
        self.assertEqual(_plan(80, True, +3.0, _LONG_KW)["direction"], "long")

    def test_long_vetoed_when_rsi_flat_or_falling(self):
        self.assertIsNone(_plan(80, True, 0.0, _LONG_KW)["direction"])
        self.assertIsNone(_plan(80, True, -3.0, _LONG_KW)["direction"])

    def test_short_kept_when_rsi_falling(self):
        self.assertEqual(_plan(20, True, -3.0, _SHORT_KW)["direction"], "short")

    def test_short_vetoed_when_rsi_flat_or_rising(self):
        self.assertIsNone(_plan(20, True, 0.0, _SHORT_KW)["direction"])
        self.assertIsNone(_plan(20, True, +3.0, _SHORT_KW)["direction"])

    def test_veto_preserves_raw_direction(self):
        # A veto still reports the native direction for the persistence check.
        plan = _plan(80, True, -3.0, _LONG_KW)
        self.assertEqual(plan["raw_direction"], "long")

    def test_none_delta_never_vetoes(self):
        # Insufficient data to compute a slope => gate is a no-op.
        self.assertEqual(_plan(80, True, None, _LONG_KW)["direction"], "long")


class GateOnlyTouchesMomentum(unittest.TestCase):
    """Turning the gate on must not bypass the other gates."""

    def test_neutral_score_still_sits_out(self):
        plan = _plan(55, True, +3.0, _LONG_KW)
        self.assertIsNone(plan["direction"])
        self.assertIsNone(plan["raw_direction"])

    def test_adx_still_rejects(self):
        plan = _plan(80, True, +3.0, {**_LONG_KW, "adx_value": 5.0})
        self.assertIsNone(plan["direction"])


if __name__ == "__main__":
    unittest.main()
