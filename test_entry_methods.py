"""
Tests for ENTRY_MODE entry placement and the generalized fill scan.

Two things are pinned here:
  1. build_entry_levels places the entry correctly for each mode — pullback
     against the signal (and byte-identical to the original inline logic),
     market at price, breakout in the signal's direction — with the stop always
     ATR_SL_MULT ATRs beyond the entry.
  2. backtest.find_fill_index reaches the entry from whichever side it sits on:
     a pullback fills on a retrace, a breakout on continuation, a market entry
     on the next bar — and the pullback path is unchanged from the original
     low<=entry / high>=entry test.

All deterministic, no network. The golden 19/75/5 characterization in
test_scoring plus the backtest characterization guard that ENTRY_MODE=pullback
(the live default) is byte-identical to before this change.

Run with:  python -m unittest test_entry_methods
"""

import unittest

import eth_report_bot as bot
import backtest as bt


# A simple structural context: price at 100, supports below, resistances above.
PRICE = 100.0
ATR = 2.0
SUPPORTS = [96.0, 90.0]
RESISTANCES = [104.0, 110.0]


class EntryModeConfig(unittest.TestCase):
    def test_default_mode_is_pullback(self):
        self.assertEqual(bot.ENTRY_MODE, "pullback")


class BuildEntryLevels(unittest.TestCase):
    def setUp(self):
        self._mode = bot.ENTRY_MODE
        self.addCleanup(lambda: setattr(bot, "ENTRY_MODE", self._mode))

    def _levels(self, mode, direction):
        bot.ENTRY_MODE = mode
        return bot.build_entry_levels(direction, PRICE, ATR, SUPPORTS, RESISTANCES)

    # --- pullback: byte-identical to the original inline formula ---
    def test_pullback_long_matches_original_formula(self):
        entry, stop, target = self._levels("pullback", "long")
        atr_pullback = PRICE - ATR * bot.PULLBACK_ATR_MULT
        expected_entry = max(atr_pullback, 96.0)   # floored at nearest support
        self.assertAlmostEqual(entry, expected_entry)
        self.assertAlmostEqual(stop, expected_entry - ATR * bot.ATR_SL_MULT)
        self.assertAlmostEqual(target, 104.0)       # nearest resistance above price
        self.assertLess(entry, PRICE)

    def test_pullback_short_matches_original_formula(self):
        entry, stop, target = self._levels("pullback", "short")
        atr_pullback = PRICE + ATR * bot.PULLBACK_ATR_MULT
        expected_entry = min(atr_pullback, 104.0)   # capped at nearest resistance
        self.assertAlmostEqual(entry, expected_entry)
        self.assertAlmostEqual(stop, expected_entry + ATR * bot.ATR_SL_MULT)
        self.assertAlmostEqual(target, 96.0)         # nearest support below price
        self.assertGreater(entry, PRICE)

    # --- market: entry at the current price ---
    def test_market_long_entry_is_price(self):
        entry, stop, target = self._levels("market", "long")
        self.assertAlmostEqual(entry, PRICE)
        self.assertAlmostEqual(stop, PRICE - ATR * bot.ATR_SL_MULT)
        self.assertAlmostEqual(target, 104.0)

    def test_market_short_entry_is_price(self):
        entry, stop, target = self._levels("market", "short")
        self.assertAlmostEqual(entry, PRICE)
        self.assertAlmostEqual(stop, PRICE + ATR * bot.ATR_SL_MULT)
        self.assertAlmostEqual(target, 96.0)

    # --- breakout: entry in the signal's direction, past the current price ---
    def test_breakout_long_entry_is_above_price(self):
        entry, stop, target = self._levels("breakout", "long")
        self.assertAlmostEqual(entry, PRICE + ATR * bot.PULLBACK_ATR_MULT)
        self.assertGreater(entry, PRICE)
        self.assertAlmostEqual(stop, entry - ATR * bot.ATR_SL_MULT)
        # target must be a resistance strictly above the (raised) entry
        self.assertGreater(target, entry)

    def test_breakout_short_entry_is_below_price(self):
        entry, stop, target = self._levels("breakout", "short")
        self.assertAlmostEqual(entry, PRICE - ATR * bot.PULLBACK_ATR_MULT)
        self.assertLess(entry, PRICE)
        self.assertAlmostEqual(stop, entry + ATR * bot.ATR_SL_MULT)
        self.assertLess(target, entry)


def _candle(ts, o, h, l, c, vol=1000.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "vol": vol}


class FindFillIndex(unittest.TestCase):
    """The signal is at index 0 (close 100). Later candles either retrace,
    continue, or hover, and we check the fill scan reaches the right bar."""

    def _series(self, highs_lows):
        candles = [_candle(0, 100, 101, 99, 100.0)]  # signal candle, close 100
        for k, (h, l) in enumerate(highs_lows, start=1):
            candles.append(_candle(k, (h + l) / 2, h, l, (h + l) / 2))
        return candles

    def test_pullback_long_fills_on_retrace(self):
        # entry below signal price -> needs a dip
        candles = self._series([(101, 100.5), (100.2, 99.0), (101, 100)])
        idx = bt.find_fill_index(candles, 0, "long", entry=99.3)
        self.assertEqual(idx, 2)   # first bar whose low (99.0) <= 99.3

    def test_pullback_long_never_fills_if_no_dip(self):
        candles = self._series([(102, 100.4), (103, 100.6)])
        self.assertIsNone(bt.find_fill_index(candles, 0, "long", entry=99.3))

    def test_breakout_long_fills_on_continuation(self):
        # entry above signal price -> needs a rally to it
        candles = self._series([(100.5, 99.8), (101.5, 100.2)])
        idx = bt.find_fill_index(candles, 0, "long", entry=101.4)
        self.assertEqual(idx, 2)   # first bar whose high (101.5) >= 101.4

    def test_breakout_short_fills_on_continuation(self):
        # entry below signal price -> needs a drop to it
        candles = self._series([(100.2, 99.5), (99.8, 98.5)])
        idx = bt.find_fill_index(candles, 0, "short", entry=98.6)
        self.assertEqual(idx, 2)   # first bar whose low (98.5) <= 98.6

    def test_market_long_fills_next_bar(self):
        # entry at signal price -> touched almost immediately
        candles = self._series([(100.5, 99.6), (101, 100)])
        idx = bt.find_fill_index(candles, 0, "long", entry=100.0)
        self.assertEqual(idx, 1)   # next bar's low (99.6) <= 100.0

    def test_respects_wait_window(self):
        candles = self._series([(101, 100.4), (101, 100.4), (100.2, 99.0)])
        # the dip is at index 3, but a 2-bar wait can't reach it
        self.assertIsNone(bt.find_fill_index(candles, 0, "long", entry=99.3, wait=2))


if __name__ == "__main__":
    unittest.main()
