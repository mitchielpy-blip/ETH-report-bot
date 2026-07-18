"""
Tests for the shared exit-management stepper (exit_manager.ManagedExit) and its
wiring into backtest.simulate_trade.

The stepper decides, bar by bar, when a filled trade exits. Two properties matter
most and are pinned here:

  * EXIT_MODEL=fixed is byte-identical to the old set-and-forget loop (original
    stop = -1R, target = +RR, stop wins a same-bar tie), and
  * EXIT_MODEL=breakeven moves the stop to entry only AFTER a bar reaches the
    trigger, so the bar that first reaches +1R can still be stopped out at the
    original stop (no intrabar lookahead).

Run with:  python -m unittest test_exit_manager
"""

import unittest
from unittest import mock

from exit_manager import ManagedExit
import backtest
import eth_report_bot as bot


class FixedModel(unittest.TestCase):
    """model='fixed' — the stop never moves; matches the old inline loop."""

    def _long(self):
        # entry 100, stop 90, target 130 -> risk 10, target = +3R
        return ManagedExit("long", 100.0, 90.0, 130.0, model="fixed")

    def test_stop_is_minus_one_r(self):
        me = self._long()
        self.assertEqual(me.on_bar(high=101.0, low=90.0, close=95.0), ("loss", 90.0))

    def test_target_is_a_win(self):
        me = self._long()
        self.assertEqual(me.on_bar(high=130.0, low=99.0, close=129.0), ("win", 130.0))

    def test_same_bar_stop_and_target_stop_wins(self):
        me = self._long()
        # bar spans both levels — conservative assumption is the stop hit first.
        self.assertEqual(me.on_bar(high=130.0, low=90.0, close=120.0), ("loss", 90.0))

    def test_open_bar_returns_none_and_stop_never_moves(self):
        me = self._long()
        # well past +1R (110) but fixed model must not move the stop.
        self.assertEqual(me.on_bar(high=125.0, low=101.0, close=120.0), (None, None))
        self.assertFalse(me.moved_to_be)
        self.assertEqual(me.current_stop, 90.0)

    def test_short_mirror(self):
        # entry 100, stop 110, target 70 -> risk 10
        me = ManagedExit("short", 100.0, 110.0, 70.0, model="fixed")
        self.assertEqual(me.on_bar(high=110.0, low=99.0, close=105.0), ("loss", 110.0))
        me2 = ManagedExit("short", 100.0, 110.0, 70.0, model="fixed")
        self.assertEqual(me2.on_bar(high=101.0, low=70.0, close=80.0), ("win", 70.0))


class BreakevenModel(unittest.TestCase):
    """model='breakeven' — stop slides to entry after +be_at_r, protecting
    subsequent bars only."""

    def _long(self, be_at_r=1.0, be_buffer_r=0.0):
        return ManagedExit("long", 100.0, 90.0, 130.0,
                           model="breakeven", be_at_r=be_at_r, be_buffer_r=be_buffer_r)

    def test_move_happens_after_trigger_bar_not_during(self):
        me = self._long()
        # Bar reaches +1R (110) AND dips to the original stop (90). The move is
        # applied only after the exit check, so this bar is still a -1R loss.
        self.assertEqual(me.on_bar(high=110.0, low=90.0, close=105.0), ("loss", 90.0))

    def test_breakeven_protects_next_bar(self):
        me = self._long()
        # Bar 1: reaches +1R, no exit -> stop moves to entry (100).
        self.assertEqual(me.on_bar(high=110.0, low=101.0, close=108.0), (None, None))
        self.assertTrue(me.moved_to_be)
        self.assertEqual(me.current_stop, 100.0)
        # Bar 2: drifts back to entry -> scratch, not a loss.
        self.assertEqual(me.on_bar(high=104.0, low=100.0, close=100.5), ("breakeven", 100.0))

    def test_below_trigger_behaves_like_fixed(self):
        me = self._long()
        # Never reaches +1R (110); a later dip to the original stop is a real loss.
        self.assertEqual(me.on_bar(high=108.0, low=101.0, close=103.0), (None, None))
        self.assertFalse(me.moved_to_be)
        self.assertEqual(me.on_bar(high=104.0, low=90.0, close=92.0), ("loss", 90.0))

    def test_buffer_leaves_cushion_past_entry(self):
        me = self._long(be_buffer_r=0.1)  # 0.1R = 1.0 price past entry
        me.on_bar(high=112.0, low=101.0, close=110.0)
        self.assertEqual(me.current_stop, 101.0)

    def test_still_wins_after_breakeven_move(self):
        me = self._long()
        me.on_bar(high=110.0, low=101.0, close=108.0)  # move to BE
        self.assertEqual(me.on_bar(high=130.0, low=108.0, close=129.0), ("win", 130.0))

    def test_short_breakeven(self):
        # entry 100, stop 110, target 70 -> risk 10; +1R favourable = 90.
        me = ManagedExit("short", 100.0, 110.0, 70.0, model="breakeven", be_at_r=1.0)
        self.assertEqual(me.on_bar(high=99.0, low=90.0, close=95.0), (None, None))
        self.assertTrue(me.moved_to_be)
        self.assertEqual(me.current_stop, 100.0)
        self.assertEqual(me.on_bar(high=100.0, low=96.0, close=99.5), ("breakeven", 100.0))


class SimulateTradeWiring(unittest.TestCase):
    """simulate_trade must reproduce fixed outcomes exactly, and flip a
    went-+1R-then-reversed trade from loss to breakeven under EXIT_MODEL=breakeven."""

    def _candles(self):
        # Signal at index 0; a fill bar; then a bar that reaches +1R and reverses
        # to entry over the following bars. entry=100 (market fill), stop=90,
        # target=130 in the plan below.
        def bar(ts, o, h, l, c):
            return {"ts": ts, "open": o, "high": h, "low": l, "close": c}
        return [
            bar(0, 100, 101, 99, 100),     # 0 signal bar
            bar(1, 100, 101, 100, 100),    # 1 entry touched (price <= 100 for long)
            bar(2, 100, 112, 101, 108),    # 2 runs to +1R (110) and beyond
            bar(3, 108, 109, 100, 101),    # 4 drifts back to entry (100)
            bar(4, 101, 105, 90, 92),      # 4 later dips to original stop (90)
        ]

    _PLAN = {"direction": "long", "entry": 100.0, "stop": 90.0, "target": 130.0, "rr": 3.0}

    def _run(self, exit_model):
        candles = self._candles()
        with mock.patch.object(bot, "EXIT_MODEL", exit_model), \
             mock.patch.object(bot, "BREAKEVEN_AT_R", 1.0), \
             mock.patch.object(bot, "BREAKEVEN_BUFFER_R", 0.0):
            # revalidate=lambda: pass-through (thesis still holds), no funding.
            return backtest.simulate_trade(
                candles, signal_index=0, plan=self._PLAN,
                revalidate=lambda idx: self._PLAN, wait=4, max_hold=10)

    def test_fixed_takes_the_original_stop_loss(self):
        outcome, net_r, _, costs, _ = self._run("fixed")
        self.assertEqual(outcome, "loss")
        self.assertAlmostEqual(costs["gross_r"], -1.0, places=6)

    def test_breakeven_scratches_the_same_trade(self):
        outcome, net_r, _, costs, _ = self._run("breakeven")
        self.assertEqual(outcome, "breakeven")
        self.assertAlmostEqual(costs["gross_r"], 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
