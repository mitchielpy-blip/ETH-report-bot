"""
Tests for the optional trading-session filter (SKIP_SESSIONS).

The filter sits out signals generated during a configured session, keyed by
the signal bar's UTC hour. It is a signal-GENERATION gate — like the R:R gate,
it must NOT fire at fill time (require_rr=False), or a pending order created in
an allowed session would be wrongly discarded just because its entry got
touched during a filtered one. These tests pin both halves down.

Run with:  python -m unittest test_session_filter
"""

import unittest
from unittest import mock
from datetime import datetime, timezone

import eth_report_bot as bot


def _ts_at_utc_hour(hour):
    """Epoch-ms for a fixed date at the given UTC hour."""
    return int(datetime(2024, 1, 1, hour, 0, tzinfo=timezone.utc).timestamp() * 1000)


class SessionForHour(unittest.TestCase):
    def test_boundaries_match_diagnostics(self):
        # asia 00-08 UTC, europe 08-16, us 16-24 — same cut points diagnostics
        # buckets by, so the filter sits out exactly the analysed buckets.
        self.assertEqual(bot.session_for_hour(0), "asia")
        self.assertEqual(bot.session_for_hour(7), "asia")
        self.assertEqual(bot.session_for_hour(8), "europe")
        self.assertEqual(bot.session_for_hour(15), "europe")
        self.assertEqual(bot.session_for_hour(16), "us")
        self.assertEqual(bot.session_for_hour(23), "us")


class SessionGate(unittest.TestCase):
    """suggest_trade_plan's session gate: sit out a filtered session, trade the
    rest, and preserve raw_direction for next hour's persistence check."""

    _KW = dict(price=100.0, atr_value=1.0, supports=[95.0], resistances=[110.0],
               htf_trend=None, adx_value=30.0, previous_raw_direction="long")

    def _plan(self, session):
        kw = dict(self._KW, score=bot.LONG_SCORE_MIN)
        return bot.suggest_trade_plan(session=session, **kw)

    def test_filtered_session_sits_out_but_keeps_raw_direction(self):
        with mock.patch.object(bot, "SKIP_SESSIONS_SET", frozenset({"asia"})):
            plan = self._plan("asia")
        self.assertIsNone(plan["direction"])
        self.assertIn("session", plan["reason"].lower())
        self.assertEqual(plan["raw_direction"], "long")  # persistence still tracked

    def test_allowed_session_trades(self):
        with mock.patch.object(bot, "SKIP_SESSIONS_SET", frozenset({"asia"})):
            plan = self._plan("europe")
        self.assertEqual(plan["direction"], "long")

    def test_session_none_is_never_filtered(self):
        # session=None (fill-time re-check) must bypass the gate entirely.
        with mock.patch.object(bot, "SKIP_SESSIONS_SET", frozenset({"asia"})):
            plan = self._plan(None)
        self.assertEqual(plan["direction"], "long")

    def test_empty_config_trades_every_session(self):
        # Default (no SKIP_SESSIONS) -> byte-unchanged behaviour, all sessions on.
        with mock.patch.object(bot, "SKIP_SESSIONS_SET", frozenset()):
            plan = self._plan("asia")
        self.assertEqual(plan["direction"], "long")


class EvaluatePlanWiring(unittest.TestCase):
    """evaluate_plan must derive the session from the signal bar's timestamp and
    pass it to suggest_trade_plan at signal time (require_rr=True), but pass
    session=None at fill time (require_rr=False)."""

    def _run(self, hour, require_rr):
        candles = [{"ts": _ts_at_utc_hour(hour), "close": 100.0}]
        with mock.patch.object(bot, "support_resistance", return_value=([95.0], [110.0])), \
             mock.patch.object(bot, "atr", return_value=1.0), \
             mock.patch.object(bot, "adx", return_value=30.0), \
             mock.patch.object(bot, "compute_bias_score", return_value=bot.LONG_SCORE_MIN), \
             mock.patch.object(bot, "suggest_trade_plan", return_value={"direction": None}) as stp:
            bot.evaluate_plan(candles, previous_raw_direction="long",
                              htf_trend=None, require_rr=require_rr)
        return stp.call_args.kwargs.get("session")

    def test_signal_time_passes_session_from_bar(self):
        self.assertEqual(self._run(hour=3, require_rr=True), "asia")
        self.assertEqual(self._run(hour=10, require_rr=True), "europe")

    def test_fill_time_passes_session_none(self):
        self.assertIsNone(self._run(hour=3, require_rr=False))


if __name__ == "__main__":
    unittest.main()
