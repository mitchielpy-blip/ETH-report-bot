"""
ETH Report Bot — Entry-Method Comparison
------------------------------------------
The live bot enters on a pullback: when a signal fires it doesn't buy at the
current price, it places a resting order a shallow ATR-scaled retrace away and
only trades if price comes back to it (and the setup still holds at fill).
That buys a better price but filters the trades hard — you miss the signals
that ran without you, and the good-looking reward:risk is measured on a fill
you might not get.

This asks whether that pullback machinery actually earns its keep, by running
the *same* signal through three different entry placements over the *same*
candles and funding, and reporting the outcomes side by side:

  * pullback  — the live method (wait for a retrace against the signal),
  * market    — take the signal now, at the current price,
  * breakout  — enter only as price extends further in the signal's direction.

Only the entry placement changes (bot.ENTRY_MODE). The direction decision,
the score, every gate (ADX, persistence, higher-timeframe trend), the R:R
floor, the fill-time re-check, costs and the one-position-at-a-time rule are
all the shared backtest logic — so this is a clean apples-to-apples read on
the entry method alone.

Like score_calibration.py, this measures; it does not change live behaviour.
The live bot and fill_checker stay on ENTRY_MODE=pullback.

Usage:
    pip install -r requirements.txt
    python entry_method_backtest.py                       # ETH, 6mo, 1H
    python entry_method_backtest.py --inst SOL-USDT-SWAP --months 12
    python entry_method_backtest.py --end-date 2024-06-01  # out-of-sample

Reading the results:
  * Expectancy (net R per trade) is the bottom line — after fees + funding.
  * Fill rate matters: a method that only fills 15% of signals is a very
    different strategy from one that fills 90%, even at the same expectancy.
  * The H1/H2 expectancy split is the honesty check — a method whose edge
    lives in one half of the window isn't one to trust. Given the score has
    no measured directional edge, expect all three to land near break-even;
    the useful finding is which one bleeds least and whether the pullback's
    extra complexity is buying anything.
"""

import argparse
import csv

import eth_report_bot as bot
import backtest as bt

MODES = ["pullback", "market", "breakout"]


def _expectancy(trades):
    """Net R per trade over `trades` (0.0 if none)."""
    if not trades:
        return 0.0
    return sum(t["r_multiple"] for t in trades) / len(trades)


def run_mode(mode, candles, funding_events):
    """Run the full backtest with ENTRY_MODE=mode and return a metrics dict.
    Restores the previous ENTRY_MODE afterwards so nothing leaks between runs."""
    prev = bot.ENTRY_MODE
    bot.ENTRY_MODE = mode
    try:
        trades, no_fill, invalidated = bt.run_backtest(candles, funding_events)
    finally:
        bot.ENTRY_MODE = prev

    total_signals = len(trades) + no_fill + invalidated
    win_rate, avg_r = bt.win_rate_and_avg_r(trades)
    _, max_dd = bt.equity_and_drawdown(trades)
    equity, _ = bt.equity_and_drawdown(trades)

    mid = len(trades) // 2
    return {
        "mode": mode,
        "signals": total_signals,
        "trades": len(trades),
        "no_fill": no_fill,
        "invalidated": invalidated,
        "fill_rate": (len(trades) / total_signals * 100) if total_signals else 0.0,
        "wins": sum(1 for t in trades if t["outcome"] == "win"),
        "losses": sum(1 for t in trades if t["outcome"] == "loss"),
        "timeouts": sum(1 for t in trades if t["outcome"] == "timeout"),
        "win_rate": win_rate,
        "avg_gross_r": (sum(t["gross_r"] for t in trades) / len(trades)) if trades else 0.0,
        "expectancy": avg_r,
        "h1_expectancy": _expectancy(trades[:mid]),
        "h2_expectancy": _expectancy(trades[mid:]),
        "max_dd": max_dd,
        "equity_end": equity[-1],
    }


def print_comparison(results):
    def row(label, fmt):
        cells = "".join(f"{fmt(r):>14}" for r in results)
        print(f"{label:<26}{cells}")

    print("\n=== Entry-method comparison (same signal, same data) ===")
    header = f"{'metric':<26}" + "".join(f"{r['mode']:>14}" for r in results)
    print("\n" + header)
    print("-" * len(header))
    row("Signals generated", lambda r: f"{r['signals']}")
    row("Filled & traded", lambda r: f"{r['trades']}")
    row("Never filled", lambda r: f"{r['no_fill']}")
    row("Invalidated at fill", lambda r: f"{r['invalidated']}")
    row("Fill rate %", lambda r: f"{r['fill_rate']:.0f}")
    print("-" * len(header))
    row("Wins / Losses / Timeouts", lambda r: f"{r['wins']}/{r['losses']}/{r['timeouts']}")
    row("Win rate % (excl TO)", lambda r: f"{r['win_rate']:.1f}")
    row("Avg gross R", lambda r: f"{r['avg_gross_r']:+.3f}")
    row("Expectancy (net R)", lambda r: f"{r['expectancy']:+.3f}")
    row("  H1 expectancy", lambda r: f"{r['h1_expectancy']:+.3f}")
    row("  H2 expectancy", lambda r: f"{r['h2_expectancy']:+.3f}")
    row("Max drawdown %", lambda r: f"{r['max_dd']:.1f}")
    row("Equity end (start 100)", lambda r: f"{r['equity_end']:.1f}")
    print("-" * len(header))
    print("\nExpectancy (net R per trade) is the bottom line. Watch the H1/H2 split: a")
    print("method whose edge sits in only one half isn't trustworthy. All three landing")
    print("near zero is the expected result given the score's measured lack of edge —")
    print("the finding is then which bleeds least and whether pullback's filtering helps.")


def save_csv(results, path="entry_method_comparison.csv"):
    if not results:
        return
    fields = ["mode", "signals", "trades", "no_fill", "invalidated", "fill_rate",
              "wins", "losses", "timeouts", "win_rate", "avg_gross_r",
              "expectancy", "h1_expectancy", "h2_expectancy", "max_dd", "equity_end"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({k: round(v, 4) if isinstance(v, float) else v for k, v in r.items()})
    print(f"\nSaved: {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inst", default=bot.INST_ID)
    parser.add_argument("--bar", default=bot.BAR)
    parser.add_argument("--months", type=float, default=6.0)
    parser.add_argument("--end-date", default=None,
                        help="Pull data ending at this date instead of now, e.g. 2024-06-01.")
    parser.add_argument("--modes", default=",".join(MODES),
                        help="Comma-separated entry modes to compare (default pullback,market,breakout).")
    args = parser.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    target_count = bt.target_count_for(args.bar, args.months)
    end_ts = bt.parse_end_ts(args.end_date)

    print(f"Fetching ~{target_count} {args.bar} candles for {args.inst}"
          + (f" ending {args.end_date}" if args.end_date else "") + " ...")
    candles = bt.fetch_historical_candles(args.inst, args.bar, target_count, end_ts)
    print(f"Got {len(candles)} candles.")

    print("Fetching real historical funding rates ...")
    funding_events = bt.fetch_funding_history(args.inst, candles[0]["ts"], candles[-1]["ts"])
    print(f"Got {len(funding_events)} funding events.")

    print(f"Comparing entry methods {modes} over the same window "
          f"(fees and funding identical across modes) ...")
    results = [run_mode(m, candles, funding_events) for m in modes]

    print_comparison(results)
    save_csv(results)

    print("\nThis script does not change eth_report_bot.py, fill_checker.py or state.json — "
          "the live bot stays on ENTRY_MODE=pullback. It only measures the alternatives.")


if __name__ == "__main__":
    main()
