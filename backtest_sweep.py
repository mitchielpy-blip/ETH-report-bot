"""
ETH Report Bot — Parameter Sweep
----------------------------------
Answers one specific question: if a single strategy threshold is changed
(e.g. a shallower pullback entry via PULLBACK_ATR_MULT, or a looser
reward:risk gate via MIN_RR), do more signals actually trade — and does
the strategy's edge (net expectancy) survive, or collapse?

This does NOT change eth_report_bot.py or its live behavior. It fetches
one shared set of historical candles + funding events, then re-runs
backtest.run_backtest() once per value in the sweep, temporarily
patching the chosen bot.<PARAM> for each pass and restoring it
afterward. Every pass uses the exact same signal logic, warmup, and
cost model as backtest.py — only the swept threshold changes.

Usage:
    pip install -r requirements.txt
    python backtest_sweep.py                          # PULLBACK_ATR_MULT: 1.0, 0.7, 0.5, 0.3
    python backtest_sweep.py --months 12
    python backtest_sweep.py --param MIN_RR --values 1.5,1.4,1.3,1.2
    python backtest_sweep.py --end-date 2024-06-01     # out-of-sample window

Output:
    Console comparison table (trade count, fill rate, win rate, expectancy,
    equity, max drawdown, first-half vs second-half split) for every value.
    <param>_sweep.csv — one row per value tested, for your own records.

Reading the results:
  - The signal/trade counts show the "trade more often" lever working
    (e.g. fill rate rises as PULLBACK_ATR_MULT drops; total signals rise
    as MIN_RR drops).
  - Watch what happens to average NET R-multiple alongside it. If
    expectancy holds roughly steady (or only dips slightly) while trade
    count climbs, more trades = a real improvement. If expectancy craters
    toward zero or negative, the stricter threshold was doing real work,
    and chasing more trades would be trading edge for frequency — not a
    free upgrade.
  - Also compare first-half vs second-half expectancy for each value, same
    as backtest.py's split check — a value that only looks good because
    one half of the window carried it is less trustworthy than one that's
    consistently positive across both halves.
"""

import argparse
import csv

import eth_report_bot as bot
import backtest as bt

# Thresholds this sweep is allowed to patch. Each is a float module global
# on eth_report_bot that suggest_trade_plan reads at call time, so patching
# the module attribute is exactly equivalent to setting the env var live.
SWEEPABLE_PARAMS = (
    "PULLBACK_ATR_MULT", "MIN_RR", "ADX_MIN", "ATR_SL_MULT",
    "LONG_SCORE_MIN", "SHORT_SCORE_MAX",
)


def run_one_sweep_value(candles, funding_events, value, param="PULLBACK_ATR_MULT"):
    """
    Runs a full backtest pass with bot.<param> temporarily set to value,
    then restores the original no matter what (even on error) so later
    sweep values, or anything else importing this module, aren't left
    with a stale global.
    """
    if param not in SWEEPABLE_PARAMS:
        raise ValueError(f"Unknown sweep param {param!r} — expected one of {SWEEPABLE_PARAMS}")
    original = getattr(bot, param)
    setattr(bot, param, value)
    try:
        trades, no_fill_count, invalidated_count = bt.run_backtest(candles, funding_events)
    finally:
        setattr(bot, param, original)

    total_signals = len(trades) + no_fill_count + invalidated_count
    fill_rate = (len(trades) / total_signals * 100) if total_signals else 0.0

    if not trades:
        return {
            param.lower(): value,
            "total_signals": total_signals,
            "trades": 0,
            "fill_rate_pct": round(fill_rate, 1),
            "win_rate_pct": None,
            "avg_net_r": None,
            "equity_final": None,
            "max_dd_pct": None,
            "first_half_avg_r": None,
            "second_half_avg_r": None,
        }

    win_rate, avg_r = bt.win_rate_and_avg_r(trades)
    equity, max_dd = bt.equity_and_drawdown(trades)

    first_half_avg_r = second_half_avg_r = None
    if len(trades) >= 10:
        mid = len(trades) // 2
        first_half_avg_r = bt.win_rate_and_avg_r(trades[:mid])[1]
        second_half_avg_r = bt.win_rate_and_avg_r(trades[mid:])[1]

    return {
        param.lower(): value,
        "total_signals": total_signals,
        "trades": len(trades),
        "fill_rate_pct": round(fill_rate, 1),
        "win_rate_pct": round(win_rate, 1),
        "avg_net_r": round(avg_r, 3),
        "equity_final": round(equity[-1], 1),
        "max_dd_pct": round(max_dd, 1),
        "first_half_avg_r": round(first_half_avg_r, 3) if first_half_avg_r is not None else None,
        "second_half_avg_r": round(second_half_avg_r, 3) if second_half_avg_r is not None else None,
    }


def print_table(results, param="PULLBACK_ATR_MULT"):
    headers = [param, "Signals", "Trades", "Fill%", "Win%", "AvgNetR", "Equity", "MaxDD%", "1stHalfR", "2ndHalfR"]
    rows = []
    for r in results:
        rows.append([
            f"{r[param.lower()]:.2f}",
            str(r["total_signals"]),
            str(r["trades"]),
            f"{r['fill_rate_pct']}" if r["fill_rate_pct"] is not None else "-",
            f"{r['win_rate_pct']}" if r["win_rate_pct"] is not None else "-",
            f"{r['avg_net_r']:+.3f}" if r["avg_net_r"] is not None else "-",
            f"{r['equity_final']}" if r["equity_final"] is not None else "-",
            f"{r['max_dd_pct']}" if r["max_dd_pct"] is not None else "-",
            f"{r['first_half_avg_r']:+.3f}" if r["first_half_avg_r"] is not None else "-",
            f"{r['second_half_avg_r']:+.3f}" if r["second_half_avg_r"] is not None else "-",
        ])

    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    def fmt_row(cells):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print("\n" + fmt_row(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt_row(row))


def save_csv(results, path):
    if not results:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"\nSaved: {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inst", default=bot.INST_ID)
    parser.add_argument("--bar", default=bot.BAR)
    parser.add_argument("--months", type=float, default=12.0)
    parser.add_argument("--end-date", default=None,
                         help="Pull data ending at this date instead of now, e.g. 2024-06-01.")
    parser.add_argument("--param", default="PULLBACK_ATR_MULT", choices=SWEEPABLE_PARAMS,
                         help="Which strategy threshold to sweep (default PULLBACK_ATR_MULT).")
    parser.add_argument("--values", default=None,
                         help="Comma-separated values to test, e.g. 1.0,0.7,0.5,0.3 "
                              "(default depends on --param: 1.0,0.7,0.5,0.3 for "
                              "PULLBACK_ATR_MULT; required for other params).")
    args = parser.parse_args()

    if args.values is None:
        if args.param == "PULLBACK_ATR_MULT":
            args.values = "1.0,0.7,0.5,0.3"
        else:
            parser.error(f"--values is required when sweeping {args.param}")
    sweep_values = [float(v.strip()) for v in args.values.split(",")]

    target_count = bt.target_count_for(args.bar, args.months)
    end_ts = bt.parse_end_ts(args.end_date)

    print(f"Fetching ~{target_count} {args.bar} candles for {args.inst}"
          + (f" ending {args.end_date}" if args.end_date else "") + " ...")
    candles = bt.fetch_historical_candles(args.inst, args.bar, target_count, end_ts)
    print(f"Got {len(candles)} candles.")

    print("Fetching real historical funding rates ...")
    funding_events = bt.fetch_funding_history(args.inst, candles[0]["ts"], candles[-1]["ts"])
    print(f"Got {len(funding_events)} funding events.")

    others = [p for p in SWEEPABLE_PARAMS if p != args.param]
    print(f"\nRunning sweep over {args.param} = {sweep_values} "
          f"(all other settings, including {'/'.join(others)}, "
          f"stay at their current eth_report_bot.py values) ...")

    results = []
    for v in sweep_values:
        print(f"  ... testing {args.param}={v}")
        results.append(run_one_sweep_value(candles, funding_events, v, param=args.param))

    print_table(results, param=args.param)
    save_csv(results, path=f"{args.param.lower()}_sweep.csv")

    print("\nReminder: a looser threshold trades more often only by accepting setups the "
          "stricter value rejected — so per-trade quality tends to shrink even before "
          "considering win rate. Compare AvgNetR and the two half-splits above, not just "
          "the trade count, before deciding a looser value is actually better. This script "
          "does not change eth_report_bot.py or state.json — it only informs what to "
          "potentially test live.")


if __name__ == "__main__":
    main()
