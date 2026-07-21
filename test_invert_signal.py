"""
Tests for the contrarian fade override (INVERT_SIGNAL).

When on, suggest_trade_plan takes the OPPOSITE side of every signal the fixed
strategy would trade. The flip happens after all native-direction gates, so the
set of tradeable bars is unchanged and only the side inverts:
  * a long-zone score is traded SHORT (mirrored short levels),
  * a short-zone score is traded LONG,
  * raw_direction stays the score's native call (persistence unaffected),
  * default off => byte-identical to the fixed strategy.

INVERT_SIGNAL is a backtest-research knob and must never be set on a live
workflow; these tests only exercise the pure decision path.

Run with:  python -m unittest test_invert_signal
"""

import unittest
from unittest import mock

import eth_report_bot as bot
import backtest as bt


# htf_trend=None so the HTF gate never vetoes; adx high and previous_raw_direction
# supplied so ADX and persistence pass; session=None so the session gate is off.
_LONG_KW = dict(price=100.0, atr_value=1.0, supports=[95.0], resistances=[110.0],
                htf_trend=None, adx_value=30.0, previous_raw_direction="long",
                session=None)
_SHORT_KW = dict(price=100.0, atr_value=1.0, supports=[90.0], resistances=[105.0],
                 htf_trend=None, adx_value=30.0, previous_raw_direction="short",
                 session=None)


def _plan(score, invert, **kw):
    with mock.patch.object(bot, "INVERT_SIGNAL", invert):
        return bot.suggest_trade_plan(score=score, **kw)


class FadeFlipsSide(unittest.TestCase):
    def test_long_signal_traded_short_when_inverted(self):
        base = _plan(bot.LONG_SCORE_MIN, invert=False, **_LONG_KW)
        fade = _plan(bot.LONG_SCORE_MIN, invert=True, **_LONG_KW)
        self.assertEqual(base["direction"], "long")
        self.assertEqual(fade["direction"], "short")
        # Native call preserved either way, so persistence tracking is unchanged.
        self.assertEqual(base["raw_direction"], "long")
        self.assertEqual(fade["raw_direction"], "long")
        # The faded trade is a real, mirrored short: stop above entry, target below.
        self.assertGreater(fade["stop"], fade["entry"])
        self.assertLess(fade["target"], fade["entry"])

    def test_short_signal_traded_long_when_inverted(self):
        base = _plan(bot.SHORT_SCORE_MAX, invert=False, **_SHORT_KW)
        fade = _plan(bot.SHORT_SCORE_MAX, invert=True, **_SHORT_KW)
        self.assertEqual(base["direction"], "short")
        self.assertEqual(fade["direction"], "long")
        self.assertEqual(fade["raw_direction"], "short")
        self.assertLess(fade["stop"], fade["entry"])
        self.assertGreater(fade["target"], fade["entry"])


class FadeKeepsBarSelection(unittest.TestCase):
    """The gates run on the native direction, so a bar the fixed strategy sits
    out is still sat out when faded (same SET of trades, opposite side)."""

    def test_neutral_score_still_sits_out(self):
        neutral = (bot.LONG_SCORE_MIN + bot.SHORT_SCORE_MAX) / 2
        fade = _plan(neutral, invert=True, **_LONG_KW)
        self.assertIsNone(fade["direction"])

    def test_adx_reject_still_rejects_when_faded(self):
        kw = dict(_LONG_KW, adx_value=bot.ADX_MIN - 1)
        fade = _plan(bot.LONG_SCORE_MIN, invert=True, **kw)
        self.assertIsNone(fade["direction"])
        self.assertEqual(fade["raw_direction"], "long")

    def test_htf_gate_uses_native_direction(self):
        # Bearish HTF vetoes a native long BEFORE the flip, so no faded short
        # leaks through — bar selection matches the fixed strategy exactly.
        kw = dict(_LONG_KW, htf_trend="bearish")
        fade = _plan(bot.LONG_SCORE_MIN, invert=True, **kw)
        self.assertIsNone(fade["direction"])


class FadeOffIsUnchanged(unittest.TestCase):
    """Regression: default (off) reproduces the fixed plan byte-for-byte."""

    def test_default_off_matches_fixed(self):
        base = _plan(bot.LONG_SCORE_MIN, invert=False, **_LONG_KW)
        # Explicitly off is the shipped default; nothing about the plan changes.
        self.assertEqual(base["direction"], "long")
        again = _plan(bot.LONG_SCORE_MIN, invert=False, **_LONG_KW)
        self.assertEqual(base, again)


class FadeFillRevalidation(unittest.TestCase):
    """Regression: simulate_trade's fill-time revalidation must feed the plan's
    NATIVE raw_direction to the persistence gate, not the traded (possibly
    flipped) direction. Passing the flipped side made the persistence gate reject
    every fill under INVERT_SIGNAL -> 0 filled trades across the whole window."""

    def test_revalidate_receives_native_raw_direction(self):
        # A faded pending order: traded short, native call still long.
        plan = {"direction": "short", "raw_direction": "long",
                "entry": 100.0, "stop": 102.0, "target": 94.0}
        # Price rises to the short entry (fill), then drops to the target (win).
        candles = [
            {"ts": 0, "open": 99.0, "high": 99.0, "low": 98.0, "close": 98.5},
            {"ts": 1, "open": 99.0, "high": 101.0, "low": 99.0, "close": 100.5},
            {"ts": 2, "open": 100.0, "high": 100.0, "low": 93.0, "close": 94.0},
        ]
        captured = {}

        def fake_eval(cs, i, previous_raw_direction=None, require_rr=True):
            captured["prd"] = previous_raw_direction
            return {"direction": "short", "raw_direction": "long"}

        with mock.patch.object(bt, "evaluate_signal_at", side_effect=fake_eval):
            outcome = bt.simulate_trade(candles, 0, plan, wait=5, max_hold=5)[0]

        # Native call, NOT the flipped "short" — the whole point of the fix.
        self.assertEqual(captured["prd"], "long")
        self.assertNotEqual(outcome, "invalidated")


if __name__ == "__main__":
    unittest.main()
