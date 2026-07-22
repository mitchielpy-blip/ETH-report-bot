"""
Tests for the modeled-funding gap fill (backtest.py).

OKX's funding-rate-history endpoint only serves a few recent months, so an
out-of-sample window's older portion came back with ZERO funding events —
silently undercharging funding drag and biasing comparisons toward
high-trade-count settings. build_funding_events() keeps every real event OKX
serves and fills the un-served older gap with modeled events on an 8h grid,
priced from recent real funding. These tests pin the gap math, the rate
estimate, and that a modeled event flows through compute_trade_costs with the
correct sign — all without hitting the network (fetch_funding_history is
patched).

Run with:  python -m unittest test_funding_model
"""

import unittest
from unittest import mock

import backtest as bt

H8 = bt.FUNDING_INTERVAL_MS          # one 8h funding interval, in ms
DAY = 24 * 3600 * 1000


class ModelFundingGap(unittest.TestCase):
    """_model_funding_gap: pure merge of real + modeled 8h-grid events."""

    def test_no_real_models_whole_window(self):
        start, end = 0, 10 * H8              # 10 intervals wide
        events, modeled = bt._model_funding_gap(start, end, [], 0.0002)
        # Grid points are the first boundary strictly after start (H8) up to
        # but not including gap_end (=end, since no real): H8..9*H8 => 9 events.
        self.assertEqual(modeled, 9)
        self.assertEqual(len(events), 9)
        self.assertTrue(all(e["rate"] == 0.0002 for e in events))
        self.assertEqual([e["ts"] for e in events], [H8 * i for i in range(1, 10)])

    def test_real_recent_half_models_only_older_gap(self):
        # Real funding covers the newest part; the older part must be modeled.
        start, end = 0, 20 * H8
        real = [{"ts": 15 * H8, "rate": 0.001}, {"ts": 16 * H8, "rate": 0.001}]
        events, modeled = bt._model_funding_gap(start, end, real, 0.0003)
        # gap_end = earliest real = 15*H8 -> model H8..14*H8 = 14 events.
        self.assertEqual(modeled, 14)
        # Real events are preserved and the whole thing is time-sorted.
        self.assertEqual(len(events), 16)
        self.assertEqual([e["ts"] for e in events], sorted(e["ts"] for e in events))
        self.assertIn({"ts": 15 * H8, "rate": 0.001}, events)
        # Modeled events carry the estimated rate, not the real one.
        modeled_events = [e for e in events if e["ts"] < 15 * H8]
        self.assertTrue(all(e["rate"] == 0.0003 for e in modeled_events))

    def test_full_real_coverage_models_nothing(self):
        # Earliest real event is within one interval of start -> no gap.
        start, end = 0, 5 * H8
        real = [{"ts": H8 // 2, "rate": 0.001}]
        events, modeled = bt._model_funding_gap(start, end, real, 0.0009)
        self.assertEqual(modeled, 0)
        self.assertEqual(events, real)


class EstimateRecentFundingRate(unittest.TestCase):
    """estimate_recent_funding_rate: mean of in-window real, else recent, else default."""

    def test_prefers_in_window_real_mean(self):
        real = [{"ts": 1, "rate": 0.001}, {"ts": 2, "rate": 0.003}]
        # No network call should be needed when in-window real exists.
        with mock.patch.object(bt, "fetch_funding_history",
                               side_effect=AssertionError("should not fetch")):
            rate = bt.estimate_recent_funding_rate("ETH-USDT-SWAP", real)
        self.assertAlmostEqual(rate, 0.002)

    def test_falls_back_to_recent_served_mean(self):
        served = [{"ts": 1, "rate": 0.004}, {"ts": 2, "rate": 0.006}]
        with mock.patch.object(bt, "fetch_funding_history", return_value=served):
            rate = bt.estimate_recent_funding_rate("ETH-USDT-SWAP", [])
        self.assertAlmostEqual(rate, 0.005)

    def test_falls_back_to_constant_when_nothing_served(self):
        with mock.patch.object(bt, "fetch_funding_history", return_value=[]):
            rate = bt.estimate_recent_funding_rate("ETH-USDT-SWAP", [])
        self.assertEqual(rate, bt.ASSUMED_FUNDING_RATE)

    def test_env_override_wins_over_data(self):
        # When ASSUMED_FUNDING_RATE is set, the explicit rate is used even if
        # real in-window funding exists — the stress-test knob, no fetch needed.
        real = [{"ts": 1, "rate": 0.001}]
        with mock.patch.object(bt, "FUNDING_RATE_OVERRIDDEN", True), \
             mock.patch.object(bt, "ASSUMED_FUNDING_RATE", 0.0005), \
             mock.patch.object(bt, "fetch_funding_history",
                               side_effect=AssertionError("should not fetch")):
            rate = bt.estimate_recent_funding_rate("ETH-USDT-SWAP", real)
        self.assertEqual(rate, 0.0005)


class BuildFundingEvents(unittest.TestCase):
    """build_funding_events: the single entry point used by every backtest tool."""

    def test_full_coverage_returns_real_untouched(self):
        # Real spans the whole window -> nothing modeled, real returned as-is.
        start, end = 0, 3 * H8
        real = [{"ts": H8 // 2, "rate": 0.001}, {"ts": 2 * H8, "rate": 0.001}]
        with mock.patch.object(bt, "fetch_funding_history", return_value=real):
            events = bt.build_funding_events("ETH-USDT-SWAP", start, end, verbose=False)
        self.assertEqual(events, real)

    def test_out_of_sample_window_is_fully_modeled(self):
        # Window entirely older than OKX retention -> in-window fetch empty, so
        # the rate comes from the "recent served" fallback fetch and the whole
        # window is modeled.
        start, end = 0, 6 * H8
        served_recent = [{"ts": 999, "rate": 0.002}]  # OKX's current funding

        calls = {"n": 0}

        def fake_fetch(inst, s, e):
            calls["n"] += 1
            return [] if calls["n"] == 1 else served_recent  # 1st: in-window (empty); 2nd: recent

        with mock.patch.object(bt, "fetch_funding_history", side_effect=fake_fetch):
            events = bt.build_funding_events("ETH-USDT-SWAP", start, end, verbose=False)

        self.assertEqual(len(events), 5)                       # H8..5*H8
        self.assertTrue(all(e["rate"] == 0.002 for e in events))
        self.assertEqual(calls["n"], 2)                        # in-window + recent


class ModeledFundingFlowsThroughCosts(unittest.TestCase):
    """A modeled event between fill and exit is charged with the right sign."""

    def test_long_pays_positive_modeled_funding(self):
        # entry 100, risk 10, one modeled +0.01% event inside the hold.
        ev = [{"ts": 5, "rate": 0.0001}]
        fee_r, funding_r = bt.compute_trade_costs("long", 100.0, 10.0, 0, 10, ev)
        # cost_price = 100 * 0.0001 = 0.01; funding_r = -0.01/10 = -0.001 (long pays)
        self.assertAlmostEqual(funding_r, -0.001)

    def test_short_receives_positive_modeled_funding(self):
        ev = [{"ts": 5, "rate": 0.0001}]
        fee_r, funding_r = bt.compute_trade_costs("short", 100.0, 10.0, 0, 10, ev)
        self.assertAlmostEqual(funding_r, +0.001)  # short receives when rate > 0

    def test_event_outside_hold_is_not_charged(self):
        ev = [{"ts": 50, "rate": 0.0001}]  # after exit_ts
        fee_r, funding_r = bt.compute_trade_costs("long", 100.0, 10.0, 0, 10, ev)
        self.assertEqual(funding_r, 0.0)


if __name__ == "__main__":
    unittest.main()
