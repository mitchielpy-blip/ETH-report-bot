"""
Forward-Test Report
--------------------
Reads signals_log.csv (every signal the live bot has actually posted,
committed automatically each hour) and checks what really happened to
each one using real OKX price data — the same fill-wait, invalidation,
and cost logic as backtest.py, but applied to real posted signals
instead of a historical replay.

This is the test a backtest alone can never give you: does live
performance actually match what the backtest predicted?

Usage:
    python forward_test_report.py

Requires signals_log.csv to exist (created automatically by
eth_report_bot.py once it's been running for a while).
"""

import csv
import sys
from datetime import datetime, timezone

import backtest as bt
import eth_report_bot as bot


def load_signals(path="signals_log.csv"):
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        print(f"{path} not found — the live bot needs to run for a while first.", file=sys.stderr)
        sys.exit(1)
    return [r for r in rows if r["direction"] in ("long", "short")]


def main():
    signals = load_signals()
    if not signals:
        print("No directional signals logged yet — nothing to check.")
        return

    earliest_ts = min(int(s["logged_at_ts"]) for s in signals)
    now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    hours_needed = (now_ts - earliest_ts) // (3600 * 1000) + bt.ENTRY_WAIT_CANDLES + bt.MAX_HOLD_CANDLES + 10

    print(f"Fetching real price history to check {len(signals)} logged signals ...")
    candles = bt.fetch_historical_candles(bot.INST_ID, bot.BAR, int(hours_needed))
    funding_events = bt.fetch_funding_history(bot.INST_ID, candles[0]["ts"], candles[-1]["ts"])
    print(f"Got {len(candles)} candles and {len(funding_events)} funding events.")

    results = []
    pending = 0

    for s in signals:
        signal_ts = int(s["logged_at_ts"])
        signal_index = next((i for i, c in enumerate(candles) if c["ts"] >= signal_ts), None)
        if signal_index is None:
            continue

        # Skip if there isn't enough future data yet to know the outcome
        if signal_index + bt.ENTRY_WAIT_CANDLES + bt.MAX_HOLD_CANDLES >= len(candles):
            pending += 1
            continue

        plan = {
            "direction": s["direction"],
            "entry": float(s["entry"]),
            "stop": float(s["stop"]),
            "target": float(s["target"]),
            "rr": float(s["rr"]) if s["rr"] else None,
        }

        outcome, r_multiple, exit_ts, costs = bt.simulate_trade(candles, signal_index, plan, funding_events)
        if outcome in ("no_fill", "invalidated"):
            results.append({"outcome": outcome, "r_multiple": 0.0})
            continue

        results.append({
            "outcome": outcome,
            "r_multiple": r_multiple,
            "gross_r": costs["gross_r"],
        })

    resolved = [r for r in results if r["outcome"] in ("win", "loss", "timeout")]
    no_fills = [r for r in results if r["outcome"] == "no_fill"]
    invalidated = [r for r in results if r["outcome"] == "invalidated"]

    print(f"\nLogged signals checked: {len(signals)}  (still pending / too recent to know: {pending})")
    print(f"Resolved: {len(resolved)}   No-fill: {len(no_fills)}   Invalidated before fill: {len(invalidated)}")
    if resolved:
        win_rate, avg_r = bt.win_rate_and_avg_r(resolved)
        print(f"Win rate (excl. timeouts): {win_rate:.1f}%")
        print(f"Average NET R-multiple per trade: {avg_r:+.2f}R")
        print("\nCompare this against your backtest's expectancy for the same period —")
        print("if they're in the same ballpark, that's real evidence the edge holds live.")
    else:
        print("No resolved trades yet — check back after more signals have had time to play out.")


if __name__ == "__main__":
    main()
