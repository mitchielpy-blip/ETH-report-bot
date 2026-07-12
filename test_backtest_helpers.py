"""
Tests for the shared backtest/reporting helpers extracted to remove
duplication: trade statistics, the equity/drawdown curve, OKX candle-row
parsing, and the candle-count / end-timestamp math the CLI tools share.

Run with:  python -m unittest test_backtest_helpers
"""

import unittest
from unittest import mock

import eth_report_bot as bot
import backtest as bt


def _candle(ts, high, low, close, open_=None, vol=1.0):
    return {
        "ts": ts,
        "open": open_ if open_ is not None else close,
        "high": high,
        "low": low,
        "close": close,
        "vol": vol,
    }


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


class SimulateTradeReturnsFillIndex(unittest.TestCase):
    """simulate_trade must report which candle actually filled the entry,
    so callers (diagnostics) don't have to re-derive the fill bar by
    re-scanning the candles — the exact duplication finding #3 removes."""

    def test_fill_index_on_a_win(self):
        # long plan; entry gets touched two bars after the signal, then wins
        plan = {"direction": "long", "entry": 100.0, "stop": 95.0, "target": 110.0, "rr": 2.0}
        candles = [
            _candle(0, 100, 100, 100),          # 0: signal bar
            _candle(1, 103, 101, 102),          # 1: low 101 > entry -> no fill
            _candle(2, 105, 99, 104),           # 2: low 99 <= entry -> FILLS here
            _candle(3, 111, 104, 110),          # 3: high 111 >= target -> win
        ]
        # keep the fill-time thesis re-check from invalidating the trade
        with mock.patch.object(bt, "evaluate_signal_at", return_value=dict(plan)):
            outcome, r_multiple, exit_ts, costs, fill_index = bt.simulate_trade(candles, 0, plan)
        self.assertEqual(outcome, "win")
        self.assertEqual(fill_index, 2)
        self.assertEqual(exit_ts, candles[3]["ts"])

    def test_no_fill_returns_none_index(self):
        plan = {"direction": "long", "entry": 100.0, "stop": 95.0, "target": 110.0, "rr": 2.0}
        # price never trades down to the entry within the wait window
        candles = [_candle(i, 205, 200, 202) for i in range(10)]
        outcome, r_multiple, exit_ts, costs, fill_index = bt.simulate_trade(candles, 0, plan)
        self.assertEqual(outcome, "no_fill")
        self.assertIsNone(fill_index)


class WalkForwardLoop(unittest.TestCase):
    """The single shared walk-forward iterator (finding #4) must honour the
    one-position-at-a-time busy gate and surface fill/exit indices, so both
    run_backtest and the diagnostics loop can be built on top of it."""

    def _dummy_candles(self):
        n = bt.WARMUP_CANDLES + 10
        return [_candle(i, 100, 100, 100) for i in range(n)]

    def test_busy_gate_skips_overlapping_signals(self):
        candles = self._dummy_candles()
        last = len(candles) - 1
        plan = {"direction": "long", "entry": 100.0, "stop": 95.0,
                "target": 110.0, "rr": 2.0, "raw_direction": "long"}

        def fake_simulate(cndls, i, pl, funding=None):
            # win that exits 3 bars later, so the next 3 signals are "busy"
            exit_index = min(i + 3, last)
            return "win", 2.0, cndls[exit_index]["ts"], {"gross_r": 2.0, "fee_r": 0.0, "funding_r": 0.0}, i + 1

        with mock.patch.object(bt, "evaluate_signal_at", return_value=dict(plan)), \
             mock.patch.object(bt, "simulate_trade", side_effect=fake_simulate):
            events = list(bt.walk_forward(candles, []))

        wins = [e for e in events if e["outcome"] == "win"]
        # first signal at WARMUP_CANDLES, then every 4th bar (3 busy + 1)
        self.assertEqual([e["signal_index"] for e in wins],
                         [bt.WARMUP_CANDLES, bt.WARMUP_CANDLES + 4, bt.WARMUP_CANDLES + 8])
        first = wins[0]
        self.assertEqual(first["fill_index"], bt.WARMUP_CANDLES + 1)
        self.assertEqual(first["exit_index"], bt.WARMUP_CANDLES + 3)

    def test_no_fill_events_counted_but_do_not_block(self):
        candles = self._dummy_candles()
        plan = {"direction": "long", "entry": 100.0, "stop": 95.0,
                "target": 110.0, "rr": 2.0, "raw_direction": "long"}

        def always_no_fill(cndls, i, pl, funding=None):
            return "no_fill", 0.0, None, None, None

        with mock.patch.object(bt, "evaluate_signal_at", return_value=dict(plan)), \
             mock.patch.object(bt, "simulate_trade", side_effect=always_no_fill):
            events = list(bt.walk_forward(candles, []))
            trades, no_fill_count, invalidated_count = bt.run_backtest(candles, [])

        # every iteration produced a signal, none of them blocked the next
        iterations = len(range(bt.WARMUP_CANDLES, len(candles) - 1))
        self.assertEqual(len(events), iterations)
        self.assertTrue(all(e["outcome"] == "no_fill" for e in events))
        self.assertTrue(all(e["exit_index"] is None for e in events))
        self.assertEqual((trades, no_fill_count, invalidated_count), ([], iterations, 0))


if __name__ == "__main__":
    unittest.main()
