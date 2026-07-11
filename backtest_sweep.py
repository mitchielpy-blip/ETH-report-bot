"""
ETH Report Bot — Pullback Depth Sweep
---------------------------------------
Answers one specific question: if pullback entries sit closer to price
(lower PULLBACK_ATR_MULT), do more signals actually fill — and does the
strategy's edge (net expectancy) survive, or collapse?

This does NOT change eth_report_bot.py or its live behavior. It fetches
one shared set of historical candles + funding events, then re-runs
backtest.run_backtest() once per PULLBACK_ATR_MULT value in the sweep,
temporarily patching bot.PULLBACK_ATR_MULT for each pass and restoring
it afterward. Every pass uses the exact same signal logic, warmup, and
cost model as backtest.py — only the pullback depth changes.

Usage:
    pip install -r requirements.txt
    python backtest_sweep.py                          # default: 1.0, 0.7, 0.5, 0.3
    python backtest_sweep.py --months 12
    python backtest_sweep.py --values 1.0,0.8,0.6,0.4,0.2
    python backtest_sweep.py --end-date 2024-06-01     # out-of-sample window

Output:
    Console comparison table (trade count, fill rate, win rate, expectancy,
    equity, max drawdown, first-half vs second-half split) for every value.
    pullback_sweep.csv — one row per value tested, for your own records.

Reading the results:
  - Fill rate should rise as PULLBACK_ATR_MULT drops (closer entries are
    easier to reach) — that's the "trade more often" lever working.
  - Watch what happens to average NET R-multiple alongside it. If
    expectancy holds roughly steady (or only dips slightly) while fill
    rate climbs, more trades = a real improvement. If expectancy craters
    toward zero or negative as the entry gets closer, the deeper pullback
    was doing real work (better risk:reward per trade), and chasing more
    trades would be trading edge for frequency — not a free upgrade.
  - Also compare first-half vs second-half expectancy for each value, same
    as backtest.py's split check — a value that only looks good because
    one half of the window carried it is less trustworthy than one that's
    consistently positive across both halves.
"""

import argparse
import csv
from datetime import datetime, timezone

import eth_report_bot as bot
import backtest as bt


def run_one_sweep_value(candles, funding_events, pullback_mult):
    """
    Runs a full backtest pass with bot.PULLBACK_ATR_MULT temporarily set
    to pullback_mult, then restores the original value no matter what
    (even on error) so later sweep values, or anything else importing
    this module, aren't left with a stale global.
    """
    original = bot.PULLBACK_ATR_MULT
    bot.PULLBACK_ATR_MULT = pullback_mult
    try:
        trades, no_fill_count, invalidated_count = bt.run_backtest(candles, funding_events)
    finally:
        bot.PULLBACK_ATR_MULT = original

    total_signals = len(trades) + no_fill_count + invalidated_count
    fill_rate = (len(trades) / total_signals * 100) if total_signals else 0.0

    if not trades:
        return {
            "pullback_atr_mult": pullback_mult,
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

    wins = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    decided = wins + losses
    win_rate = (len(wins) / len(decided) * 100) if decided else 0.0
    avg_r = sum(t["r_multiple"] for t in trades) / len(trades)

    equity = [100.0]
    for t in trades:
        equity.append(equity[-1] * (1 + t["r_multiple"] * bt.RISK_PER_TRADE_PCT / 100))
    peak, max_dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)

    first_half_avg_r = second_half_avg_r = None
    if len(trades) >= 10:
        mid = len(trades) // 2
        first_half, second_half = trades[:mid], trades[mid:]
        first_half_avg_r = sum(t["r_multiple"] for t in first_half) / len(first_half)
        second_half_avg_r = sum(t["r_multiple"] for t in second_half) / len(second_half)

    return {
        "pullback_atr_mult": pullback_mult,
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


def print_table(results):
    headers = ["PULLBACK_MULT", "Signals", "Trades", "Fill%", "Win%", "AvgNetR", "Equity", "MaxDD%", "1stHalfR", "2ndHalfR"]
    rows = []
    for r in results:
        rows.append([
            f"{r['pullback_atr_mult']:.2f}",
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


def save_csv(results, path="pullback_sweep.csv"):
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
    parser.add_argument("--values", default="1.0,0.7,0.5,0.3",
                         help="Comma-separated PULLBACK_ATR_MULT values to test, e.g. 1.0,0.7,0.5,0.3")
    args = parser.parse_args()

    pullback_values = [float(v.strip()) for v in args.values.split(",")]

    bars_per_month = {"1H": 24 * 30, "15m": 24 * 4 * 30, "4H": 6 * 30}
    target_count = int(bars_per_month.get(args.bar, 24 * 30) * args.months) + bt.WARMUP_CANDLES

    end_ts = None
    if args.end_date:
        end_dt = datetime.strptime(args.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_ts = int(end_dt.timestamp() * 1000)

    print(f"Fetching ~{target_count} {args.bar} candles for {args.inst}"
          + (f" ending {args.end_date}" if args.end_date else "") + " ...")
    candles = bt.fetch_historical_candles(args.inst, args.bar, target_count, end_ts)
    print(f"Got {len(candles)} candles.")

    print("Fetching real historical funding rates ...")
    funding_events = bt.fetch_funding_history(args.inst, candles[0]["ts"], candles[-1]["ts"])
    print(f"Got {len(funding_events)} funding events.")

    print(f"\nRunning sweep over PULLBACK_ATR_MULT = {pullback_values} "
          f"(all other settings, including LONG_SCORE_MIN/SHORT_SCORE_MAX/MIN_RR/ADX_MIN, "
          f"stay at their current eth_report_bot.py values) ...")

    results = []
    for v in pullback_values:
        print(f"  ... testing PULLBACK_ATR_MULT={v}")
        results.append(run_one_sweep_value(candles, funding_events, v))

    print_table(results)
    save_csv(results)

    print("\nReminder: a looser (lower) pullback multiplier fills more often only because "
          "entries sit closer to current price at signal time — that inherently means less "
          "room to the stop, so risk:reward per trade tends to shrink even before considering "
          "win rate. Compare AvgNetR and the two half-splits above, not just the trade count, "
          "before deciding a lower value is actually better. This script does not change "
          "eth_report_bot.py or state.json — it only informs what to potentially test live.")


if __name__ == "__main__":
    main()
