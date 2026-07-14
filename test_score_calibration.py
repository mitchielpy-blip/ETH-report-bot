"""
Tests for the bias-score calibration diagnostic.

The fetch/scoring path is exercised by the live bot and backtest already; what
this pins is the pure aggregation math — bucketing, up-rate, lift vs base, the
half-split, the LONG/SHORT threshold zones, and the score<->return correlation
sign — since that math is what the verdict is read off. All deterministic, no
network, no RNG.

Run with:  python -m unittest test_score_calibration
"""

import unittest

import eth_report_bot as bot
import score_calibration as sc


class TestForwardReturn(unittest.TestCase):
    def test_forward_return_is_relative(self):
        candles = [{"close": 100.0}, {"close": 105.0}, {"close": 99.0}]
        self.assertAlmostEqual(sc.forward_return(candles, 0, 1), 0.05)
        self.assertAlmostEqual(sc.forward_return(candles, 0, 2), -0.01)


class TestBucketLabel(unittest.TestCase):
    def test_edges_map_to_expected_buckets(self):
        # SHORT zone (<=45) is exactly the first two buckets; LONG zone (>=62)
        # the last two — the whole reason the edges sit on the thresholds.
        self.assertEqual(sc.bucket_label(5), "5-34")
        self.assertEqual(sc.bucket_label(45), "35-45")
        self.assertEqual(sc.bucket_label(46), "46-54")
        self.assertEqual(sc.bucket_label(61), "55-61")
        self.assertEqual(sc.bucket_label(62), "62-74")
        self.assertEqual(sc.bucket_label(95), "75-95")


class TestSummarizeIsForecastAware(unittest.TestCase):
    def _perfect_obs(self):
        """A dataset where score cleanly predicts direction: high scores always
        rise, low scores always fall, mid scores are a coin-flip."""
        obs = []
        obs += [(90, +0.02)] * 50   # deep long -> up
        obs += [(65, +0.01)] * 50   # long zone -> up
        obs += [(55, +0.01)] * 25 + [(55, -0.01)] * 25   # neutral -> 50/50
        obs += [(40, -0.01)] * 50   # short zone -> down
        obs += [(20, -0.02)] * 50   # deep short -> down
        return obs

    def test_long_zone_beats_base_short_zone_trails(self):
        s = sc.summarize(self._perfect_obs())
        self.assertGreater(s["long_zone"]["lift"], 0)     # longs above base
        self.assertLess(s["short_zone"]["lift"], 0)       # shorts below base
        # And the long zone's raw up-rate should be far above the short zone's.
        self.assertGreater(s["long_zone"]["up_rate"], s["short_zone"]["up_rate"] + 40)

    def test_correlation_positive_when_score_forecasts(self):
        s = sc.summarize(self._perfect_obs())
        self.assertGreater(s["corr"], 0.5)

    def test_flat_relationship_has_no_lift_or_correlation(self):
        # Same forward-return distribution regardless of score -> the score
        # forecasts nothing: near-zero lift on both zones, near-zero corr.
        obs = []
        for score in (20, 40, 55, 65, 90):
            obs += [(score, +0.01)] * 25 + [(score, -0.01)] * 25
        s = sc.summarize(obs)
        self.assertAlmostEqual(s["long_zone"]["lift"], 0.0, delta=1.0)
        self.assertAlmostEqual(s["short_zone"]["lift"], 0.0, delta=1.0)
        self.assertAlmostEqual(s["corr"], 0.0, delta=0.05)

    def test_zones_use_live_thresholds(self):
        obs = [(bot.LONG_SCORE_MIN, +0.01), (bot.SHORT_SCORE_MAX, -0.01),
               (55, +0.01), (55, -0.01)]
        s = sc.summarize(obs)
        self.assertEqual(s["long_zone"]["n"], 1)
        self.assertEqual(s["short_zone"]["n"], 1)
        self.assertEqual(s["neutral_zone"]["n"], 2)
        self.assertEqual(s["long_thr"], bot.LONG_SCORE_MIN)
        self.assertEqual(s["short_thr"], bot.SHORT_SCORE_MAX)

    def test_bucket_counts_cover_all_observations(self):
        obs = self._perfect_obs()
        s = sc.summarize(obs)
        self.assertEqual(sum(b["n"] for b in s["buckets"]), len(obs))
        self.assertEqual(s["n"], len(obs))


if __name__ == "__main__":
    unittest.main()
