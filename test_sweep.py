"""
Tests for backtest_sweep's generic parameter patching.

The sweep temporarily patches a strategy threshold on eth_report_bot and
must (a) patch the *chosen* param, (b) have the patched value visible to
the backtest pass while it runs, and (c) always restore the original —
even when the pass blows up — so later sweep values and other importers
never see a stale global.

Run with:  python -m unittest test_sweep
"""

import unittest
from unittest import mock

import eth_report_bot as bot
import backtest as bt
import backtest_sweep as sweep


class GenericParamPatching(unittest.TestCase):
    def test_patches_chosen_param_during_run_and_restores_after(self):
        seen = {}

        def fake_run_backtest(candles, funding_events):
            seen["MIN_RR"] = bot.MIN_RR
            seen["PULLBACK_ATR_MULT"] = bot.PULLBACK_ATR_MULT
            return [], 0, 0

        original_min_rr = bot.MIN_RR
        original_pullback = bot.PULLBACK_ATR_MULT
        with mock.patch.object(bt, "run_backtest", side_effect=fake_run_backtest):
            result = sweep.run_one_sweep_value([], [], 1.3, param="MIN_RR")

        self.assertEqual(seen["MIN_RR"], 1.3)                          # patched during the pass
        self.assertEqual(seen["PULLBACK_ATR_MULT"], original_pullback)  # others untouched
        self.assertEqual(bot.MIN_RR, original_min_rr)                   # restored afterward
        self.assertEqual(result["min_rr"], 1.3)                         # row keyed by the param

    def test_restores_param_even_when_backtest_raises(self):
        original = bot.MIN_RR
        with mock.patch.object(bt, "run_backtest", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                sweep.run_one_sweep_value([], [], 1.2, param="MIN_RR")
        self.assertEqual(bot.MIN_RR, original)

    def test_unknown_param_is_rejected(self):
        with self.assertRaises(ValueError):
            sweep.run_one_sweep_value([], [], 1.0, param="RISK_PER_TRADE_PCT")


if __name__ == "__main__":
    unittest.main()
