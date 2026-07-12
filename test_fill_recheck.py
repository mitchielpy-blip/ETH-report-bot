"""
Tests for fill_checker's fill-time re-validation.

The backtest only counts a pullback fill as a real trade after re-checking
that the setup still holds at fill time (backtest.simulate_trade). The live
fill checker must apply the SAME gate, or the backtest — which discards those
"invalidated" fills — silently overstates performance versus what the bot
actually trades. These tests pin that parity down.

Run with:  python -m unittest test_fill_recheck
"""

import unittest
from unittest import mock
from datetime import datetime, timezone

import eth_report_bot as bot
import fill_checker as fc


def _live_long_state():
    """A pending, unfilled, not-yet-expired long order."""
    return {
        "direction": "long",
        "entry": 100.0,
        "stop": 95.0,
        "target": 110.0,
        "rr": 2.0,
        "generated_at_ts": int(datetime.now(timezone.utc).timestamp() * 1000),
        "filled": False,
    }


class FillReCheck(unittest.TestCase):
    def _run(self, ticker_price, fresh_plan):
        """Drive fill_checker.main() with the network fully mocked and capture
        what it posts to Discord and what it writes back to state."""
        posted, saved = [], {}
        with mock.patch.object(bot, "load_state", return_value=_live_long_state()), \
             mock.patch.object(bot, "fetch_ticker_price", return_value=ticker_price), \
             mock.patch.object(bot, "fetch_candles", return_value=["candles"]), \
             mock.patch.object(bot, "evaluate_plan", return_value=fresh_plan) as ep, \
             mock.patch.object(bot, "post_to_discord", side_effect=posted.append), \
             mock.patch.object(bot, "save_state", side_effect=saved.update):
            fc.main()
        return posted, saved, ep

    def test_still_valid_setup_posts_fill_and_marks_filled(self):
        still_long = {"direction": "long", "entry": 100.0, "stop": 95.0,
                      "target": 110.0, "rr": 2.0, "raw_direction": "long"}
        posted, saved, _ = self._run(ticker_price=99.0, fresh_plan=still_long)
        self.assertEqual(len(posted), 1)
        self.assertIn("filled", posted[0].lower())
        self.assertTrue(saved.get("filled"))

    def test_invalidated_setup_skips_post_and_discards_order(self):
        # by fill time the score has drifted back to neutral -> no direction
        now_neutral = {"direction": None, "reason": "score neutral", "raw_direction": None}
        posted, saved, _ = self._run(ticker_price=99.0, fresh_plan=now_neutral)
        self.assertEqual(posted, [])                 # no fill alert for a dead setup
        self.assertIsNone(saved.get("direction"))    # pending order discarded
        self.assertNotEqual(saved.get("filled"), True)

    def test_flipped_setup_is_also_invalidated(self):
        now_short = {"direction": "short", "entry": 100.0, "stop": 105.0,
                     "target": 90.0, "rr": 2.0, "raw_direction": "short"}
        posted, saved, _ = self._run(ticker_price=99.0, fresh_plan=now_short)
        self.assertEqual(posted, [])
        self.assertIsNone(saved.get("direction"))

    def test_recheck_passes_pending_direction_as_persistence(self):
        # the re-check must feed the pending direction in as
        # previous_raw_direction, exactly like backtest.simulate_trade, so it
        # only asks "did the raw signal flip?" rather than re-requiring a fresh
        # two-hour confirmation at fill time.
        still_long = {"direction": "long", "raw_direction": "long"}
        _, _, ep = self._run(ticker_price=99.0, fresh_plan=still_long)
        args, kwargs = ep.call_args
        passed = kwargs.get("previous_raw_direction", args[1] if len(args) > 1 else None)
        self.assertEqual(passed, "long")

    def test_no_recheck_when_level_not_reached(self):
        # price hasn't touched the entry -> nothing to re-check, nothing posted
        with mock.patch.object(bot, "load_state", return_value=_live_long_state()), \
             mock.patch.object(bot, "fetch_ticker_price", return_value=105.0), \
             mock.patch.object(bot, "evaluate_plan") as ep, \
             mock.patch.object(bot, "post_to_discord", side_effect=AssertionError("should not post")), \
             mock.patch.object(bot, "save_state", lambda *a, **k: None):
            fc.main()
        ep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
