"""
Tests for the shared backtest/reporting helpers extracted to remove
duplication: trade statistics, the equity/drawdown curve, OKX candle-row
parsing, and the candle-count / end-timestamp math the CLI tools share.

Run with:  python -m unittest test_backtest_helpers
"""

import unittest

import eth_report_bot as bot
import backtest as bt


class WinRateAndAvgR(unittest.TestCase):
    def test_mixed_outcomes(self):
        trades = [
            {"outcome": "win", "r_multiple": 2.0},
            {"outcome": "loss", "r_multiple": -1.0},
            {"outcome": "win", "r_multiple": 1.0},
            {"outcome": "timeout", "r_multiple": 0.5},
        ]
        win_rate, avg_r = bt.win_rate_and_avg_r(trades)
        # win rate excludes the timeout: 2 wins / 3 decided
        self.assertAlmostEqual(win_rate, 2 / 3 * 100)
        # avg R is over every trade
        self.assertAlmostEqual(avg_r, (2.0 - 1.0 + 1.0 + 0.5) / 4)

    def test_empty(self):
        self.assertEqual(bt.win_rate_and_avg_r([]), (0.0, 0.0))

    def test_custom_r_key(self):
        trades = [{"outcome": "win", "net_r": 1.0}]
        win_rate, avg_r = bt.win_rate_and_avg_r(trades, r_key="net_r")
        self.assertEqual((win_rate, avg_r), (100.0, 1.0))


class EquityAndDrawdown(unittest.TestCase):
    def test_curve_and_drawdown(self):
        trades = [{"r_multiple": 10.0}, {"r_multiple": -5.0}]
        equity, max_dd = bt.equity_and_drawdown(trades)  # default 1% risk
        self.assertEqual(len(equity), 3)
        for got, want in zip(equity, [100.0, 110.0, 104.5]):
            self.assertAlmostEqual(got, want)
        self.assertAlmostEqual(max_dd, (110.0 - 104.5) / 110.0 * 100)

    def test_empty_is_flat(self):
        equity, max_dd = bt.equity_and_drawdown([])
        self.assertEqual(equity, [100.0])
        self.assertEqual(max_dd, 0.0)


class ParseCandleRow(unittest.TestCase):
    def test_okx_row(self):
        row = ["1700000000000", "2000.5", "2010", "1990", "2005", "1234.5", "x", "y", "1"]
        self.assertEqual(bot.parse_candle_row(row), {
            "ts": 1700000000000,
            "open": 2000.5,
            "high": 2010.0,
            "low": 1990.0,
            "close": 2005.0,
            "vol": 1234.5,
        })


class CandleCountMath(unittest.TestCase):
    def test_known_bars(self):
        self.assertEqual(bt.target_count_for("1H", 1.0), 720 + bt.WARMUP_CANDLES)
        self.assertEqual(bt.target_count_for("15m", 1.0), 2880 + bt.WARMUP_CANDLES)
        self.assertEqual(bt.target_count_for("4H", 1.0), 180 + bt.WARMUP_CANDLES)

    def test_unknown_bar_falls_back_to_1h(self):
        self.assertEqual(bt.target_count_for("1D", 1.0), 720 + bt.WARMUP_CANDLES)

    def test_parse_end_ts(self):
        self.assertIsNone(bt.parse_end_ts(None))
        self.assertEqual(bt.parse_end_ts("2024-06-01"), 1717200000000)


if __name__ == "__main__":
    unittest.main()
