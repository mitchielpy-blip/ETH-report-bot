"""
ETH/BTC Report Bot — Trade Diagnostics
----------------------------------------
Runs the exact same walk-forward backtest as backtest.py, but records the
market conditions present at each signal (ADX, RSI, volatility, session,
HTF alignment, direction, fill delay...) and then breaks results down by
those conditions. The goal: turn "the strategy underperforms on BTC" into
"it fails specifically when X" — which is what produces a testable
hypothesis instead of blind re-tuning.

Usage:
    python diagnostics.py --inst BTC-USDT-SWAP --months 12
    python diagnostics.py --inst BTC-USDT-SWAP --months 12 --end-date 2025-07-12
    python diagnostics.py --inst BTC-USDT-SWAP --months 12 --pullback 1.0

Outputs:
    Console breakdown tables (win rate + avg net R per condition bucket)
    diagnostic_trades.csv — every filled trade with its full context row

How to read the output:
  - AvgNetR is the number that matters; win rate without R is misleading.
  - IGNORE buckets with tiny samples (the tables print N for a reason —
    a 3-trade bucket tells you nothing, whatever its numbers say).
  - You're looking for LARGE, CONSISTENT differences: e.g. "longs +0.4R,
    shorts -0.3R" or "ADX 20-25 loses, ADX 30+ wins" across a decent N.
  - A promising split found here is a HYPOTHESIS, not a conclusion — it
    must then survive the same in-sample + out-of-sample gauntlet the
    pullback sweep went through before it's allowed anywhere near the
    live bot. Slicing one dataset many ways guarantees some slice looks
    good by chance; out-of-sample confirmation is what separates signal
    from that noise.
"""

import argparse
import csv
from datetime import datetime, timezone

import eth_report_bot as bot
import backtest as bt


# ----------------------------------------------------------------------
# Context capture: everything we know at signal time / fill time
# ----------------------------------------------------------------------

def signal_context(candles, i):
    """Market conditions at candles[i], using only data up to i (no lookahead)."""
    window = candles[:i + 1]
    closes = [c["close"] for c in window]
    price = closes[-1]

    r = bot.rsi(closes)
    _, _, hist = bot.macd(closes)
    ema20 = bot.ema_last(closes, 20)
    ema50 = bot.ema_last(closes, 50)
    atr_value = bot.atr(window)
    adx_value = bot.adx(window)
    vol_ratio = bot.volume_ratio(window)

    htf_trend = bot.htf_trend_from_closes([c["close"] for c in bt.resample_htf(window)])

    ts = candles[i]["ts"]
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

    return {
        "signal_ts": ts,
        "utc_hour": dt.hour,
        "weekday": dt.strftime("%a"),
        "price": price,
        "rsi": r,
        "macd_hist": hist,
        "ema_trend": "bull" if ema20 > ema50 else "bear",
        "htf_trend": htf_trend,
        "adx": adx_value,
        "atr_pct": (atr_value / price * 100) if price else None,  # volatility as % of price
        "vol_ratio": vol_ratio,
    }


def bucket_adx(adx):
    if adx is None:
        return "unknown"
    if adx < 25:
        return "20-25 (weak trend)"
    if adx < 30:
        return "25-30"
    if adx < 40:
        return "30-40"
    return "40+ (very strong)"


def bucket_rsi(rsi):
    if rsi is None:
        return "unknown"
    if rsi < 40:
        return "<40"
    if rsi < 50:
        return "40-50"
    if rsi < 60:
        return "50-60"
    if rsi < 70:
        return "60-70"
    return "70+"


def bucket_session(utc_hour):
    # Rough global sessions by UTC hour. SGT = UTC+8.
    if 0 <= utc_hour < 8:
        return "Asia (08-16 SGT)"
    if 8 <= utc_hour < 16:
        return "Europe (16-24 SGT)"
    return "US (00-08 SGT)"


def bucket_atr_pct(atr_pct, low_threshold, high_threshold):
    if atr_pct is None:
        return "unknown"
    if atr_pct < low_threshold:
        return "low vol"
    if atr_pct > high_threshold:
        return "high vol"
    return "mid vol"


def bucket_fill_delay(bars):
    if bars <= 1:
        return "fast fill (<=1 bar)"
    if bars <= 3:
        return "2-3 bars"
    return "4+ bars (slow)"


# ----------------------------------------------------------------------
# Backtest loop with context capture (mirrors bt.run_backtest exactly)
# ----------------------------------------------------------------------

def run_diagnostic_backtest(candles, funding_events):
    enriched_trades = []
    busy_until = -1
    previous_raw_direction = None

    for i in range(bt.WARMUP_CANDLES, len(candles) - 1):
        plan = bt.evaluate_signal_at(candles, i, previous_raw_direction)
        previous_raw_direction = plan.get("raw_direction") if plan else None

        if i <= busy_until:
            continue
        if not plan or not plan["direction"]:
            continue

        outcome, r_multiple, exit_ts, costs = bt.simulate_trade(candles, i, plan, funding_events)
        if outcome in ("no_fill", "invalidated"):
            continue

        ctx = signal_context(candles, i)

        # fill delay: find the fill bar the same way simulate_trade does
        fill_bars = None
        for j in range(i + 1, min(i + 1 + bt.ENTRY_WAIT_CANDLES, len(candles))):
            c = candles[j]
            if plan["direction"] == "long" and c["low"] <= plan["entry"]:
                fill_bars = j - i
                break
            if plan["direction"] == "short" and c["high"] >= plan["entry"]:
                fill_bars = j - i
                break

        hold_bars = None
        if exit_ts is not None and fill_bars is not None:
            exit_index = next((k for k in range(i + 1, len(candles)) if candles[k]["ts"] == exit_ts), None)
            if exit_index is not None:
                hold_bars = exit_index - (i + fill_bars)

        enriched_trades.append({
            **ctx,
            "direction": plan["direction"],
            "entry": plan["entry"],
            "planned_rr": plan["rr"],
            "outcome": outcome,
            "net_r": r_multiple,
            "gross_r": costs["gross_r"],
            "fill_delay_bars": fill_bars,
            "hold_bars": hold_bars,
        })

        exit_index = next((k for k in range(i + 1, len(candles)) if candles[k]["ts"] == exit_ts),
                          i + bt.ENTRY_WAIT_CANDLES + bt.MAX_HOLD_CANDLES)
        busy_until = exit_index

    return enriched_trades


# ----------------------------------------------------------------------
# Aggregation / reporting
# ----------------------------------------------------------------------

def breakdown(trades, key_fn, title):
    groups = {}
    for t in trades:
        k = key_fn(t)
        groups.setdefault(k, []).append(t)

    print(f"\n--- {title} ---")
    header = f"{'bucket':<22} {'N':>4} {'win%':>6} {'avgNetR':>8} {'totalR':>8}"
    print(header)
    print("-" * len(header))
    for k in sorted(groups.keys(), key=str):
        ts = groups[k]
        wins = [t for t in ts if t["outcome"] == "win"]
        losses = [t for t in ts if t["outcome"] == "loss"]
        decided = wins + losses
        win_rate = len(wins) / len(decided) * 100 if decided else 0.0
        avg_r = sum(t["net_r"] for t in ts) / len(ts)
        total_r = sum(t["net_r"] for t in ts)
        flag = "  (!) tiny sample" if len(ts) < 8 else ""
        print(f"{str(k):<22} {len(ts):>4} {win_rate:>5.1f}% {avg_r:>+8.3f} {total_r:>+8.2f}{flag}")


def save_csv(trades, path="diagnostic_trades.csv"):
    if not trades:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        w.writeheader()
        for t in trades:
            row = dict(t)
            row["signal_ts"] = datetime.fromtimestamp(t["signal_ts"] / 1000, tz=timezone.utc).isoformat()
            w.writerow(row)
    print(f"\nSaved: {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inst", default=bot.INST_ID)
    parser.add_argument("--bar", default=bot.BAR)
    parser.add_argument("--months", type=float, default=12.0)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--pullback", type=float, default=None,
                         help="PULLBACK_ATR_MULT to diagnose (defaults to the bot's current value)")
    args = parser.parse_args()

    if args.pullback is not None:
        bot.PULLBACK_ATR_MULT = args.pullback
    print(f"Diagnosing {args.inst} @ PULLBACK_ATR_MULT={bot.PULLBACK_ATR_MULT}")

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

    print("Fetching funding history ...")
    funding_events = bt.fetch_funding_history(args.inst, candles[0]["ts"], candles[-1]["ts"])
    print(f"Got {len(funding_events)} funding events.")

    print("Running diagnostic backtest ...")
    trades = run_diagnostic_backtest(candles, funding_events)
    print(f"\nFilled trades captured: {len(trades)}")
    if not trades:
        print("No trades to diagnose in this window.")
        return

    total_r = sum(t["net_r"] for t in trades)
    print(f"Total net R across all trades: {total_r:+.2f}  (avg {total_r/len(trades):+.3f}R/trade)")

    # volatility thresholds from this sample's own distribution (tercile-ish)
    atr_pcts = sorted(t["atr_pct"] for t in trades if t["atr_pct"] is not None)
    low_thr = atr_pcts[len(atr_pcts) // 3] if atr_pcts else 0
    high_thr = atr_pcts[2 * len(atr_pcts) // 3] if atr_pcts else 0
    print(f"(Volatility buckets from sample terciles: low < {low_thr:.3f}% <= mid <= {high_thr:.3f}% < high)")

    breakdown(trades, lambda t: t["direction"], "By direction")
    breakdown(trades, lambda t: bucket_adx(t["adx"]), "By ADX at signal (trend strength)")
    breakdown(trades, lambda t: bucket_rsi(t["rsi"]), "By RSI at signal")
    breakdown(trades, lambda t: t["htf_trend"] or "unknown", "By 4H higher-timeframe trend")
    breakdown(trades, lambda t: bucket_session(t["utc_hour"]), "By session")
    breakdown(trades, lambda t: t["weekday"], "By weekday")
    breakdown(trades, lambda t: bucket_atr_pct(t["atr_pct"], low_thr, high_thr), "By volatility regime (ATR as % of price)")
    breakdown(trades, lambda t: bucket_fill_delay(t["fill_delay_bars"] or 99), "By fill delay (how long entry took to get hit)")

    save_csv(trades)

    print("\nReminder: buckets marked '(!) tiny sample' are not evidence of anything. "
          "Any promising split found here is a hypothesis — it must be re-tested "
          "in-sample AND out-of-sample (like the pullback sweep was) before being "
          "trusted, because slicing one dataset many ways guarantees some slice "
          "looks good by pure chance.")


if __name__ == "__main__":
    main()
