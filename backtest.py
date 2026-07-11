"""
ETH Report Bot — Backtester
----------------------------
Replays the exact same signal logic used by eth_report_bot.py against
historical OKX candles, walking forward candle-by-candle so no future
data leaks into any decision (no lookahead bias). For every signal that
fires, it simulates forward until price hits the stop-loss or take-profit
(or a max hold period times out) and records the real outcome.

This is what actually tells you whether the bot's rules are any good —
tuning thresholds without this is just guessing.

Usage:
    pip install -r requirements.txt
    python backtest.py                     # defaults: ETH-USDT-SWAP, 1H, ~6 months
    python backtest.py --months 3           # shorter window
    python backtest.py --bar 15m --months 2 # different timeframe

Outputs:
    trades.csv     — every simulated trade with entry/exit/outcome
    equity_curve.png — cumulative equity assuming 1% risk per trade
    Console summary — win rate, avg RR, expectancy, max drawdown

CHANGELOG (audit fixes):
  * resample_htf now anchors 4H buckets to real 4H UTC boundaries
    (timestamp-based) instead of grouping from the start of a growing
    window, and drops the trailing incomplete bucket. Previously the
    backtest's "4H" candles shifted alignment every hour and never
    matched the actual 4H candles the live bot fetches from OKX.
  * EMA fallback logic unified with the live bot via bot.ema_last so
    both compute EMA20/EMA50 identically.
"""

import argparse
import csv
import os
import time
import sys
from datetime import datetime, timezone

import requests

import eth_report_bot as bot

OKX_BASE = "https://www.okx.com"
PAGE_LIMIT = 100          # OKX history-candles max per request
MAX_HOLD_CANDLES = 72     # give a filled trade up to 72 bars to hit SL/TP before timing out
ENTRY_WAIT_CANDLES = int(os.environ.get("ENTRY_WAIT_HOURS", 8))   # give a pending pullback order this many bars to actually fill (assumes 1H bars)
WARMUP_CANDLES = 260      # candles needed before the first signal can be evaluated
RISK_PER_TRADE_PCT = 1.0  # for the equity curve simulation only

# Trading cost assumptions — these are approximate OKX USDT-margined perp
# rates for a regular (non-VIP) account. Check your actual fee tier under
# Account -> Fees on OKX and adjust if different.
ENTRY_FEE_PCT = 0.02   # pullback entry is a resting limit order -> maker fee
EXIT_FEE_PCT = 0.05    # stop-loss/take-profit triggers execute as market -> taker fee

HTF_GROUP_HOURS = 4
HTF_GROUP_MS = HTF_GROUP_HOURS * 3600 * 1000


def fetch_historical_candles(inst_id, bar, target_count, end_ts=None):
    """
    Paginate OKX's history-candles endpoint backward in time until we have
    target_count candles or run out of history. Returns oldest -> newest.

    end_ts: optional millisecond timestamp to start counting back from
    (instead of "now"). Use this to pull an earlier, out-of-sample window
    that wasn't used when tuning the strategy.
    """
    all_rows = []
    after = str(end_ts) if end_ts else None  # ts cursor; None = start from most recent

    while len(all_rows) < target_count:
        params = {"instId": inst_id, "bar": bar, "limit": str(PAGE_LIMIT)}
        if after:
            params["after"] = after

        resp = requests.get(f"{OKX_BASE}/api/v5/market/history-candles", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "0":
            raise RuntimeError(f"OKX error: {data}")
        rows = data["data"]
        if not rows:
            break  # no more history available

        all_rows.extend(rows)
        after = rows[-1][0]  # oldest ts in this batch -> fetch older next
        time.sleep(0.15)     # be polite to the rate limit

    all_rows.reverse()  # oldest -> newest
    candles = [
        {
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "vol": float(row[5]),
        }
        for row in all_rows[-target_count:]
    ]
    return candles


def fetch_funding_history(inst_id, start_ts, end_ts):
    """
    Fetch real historical funding rate events for inst_id between
    start_ts and end_ts (ms). Perpetual swaps pay/receive funding on a
    schedule (commonly every 8h, though OKX has moved toward variable
    intervals) — this pulls the actual realized rates so the backtest
    can charge/credit them instead of guessing.
    """
    events = []
    after = None
    while True:
        params = {"instId": inst_id, "limit": "100"}
        if after:
            params["after"] = after
        resp = requests.get(f"{OKX_BASE}/api/v5/public/funding-rate-history", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "0":
            raise RuntimeError(f"OKX funding-rate error: {data}")
        rows = data["data"]
        if not rows:
            break
        events.extend(rows)
        oldest_ts = int(rows[-1]["fundingTime"])
        after = rows[-1]["fundingTime"]
        time.sleep(0.15)
        if oldest_ts <= start_ts:
            break

    out = [
        {"ts": int(r["fundingTime"]), "rate": float(r["fundingRate"])}
        for r in events
        if start_ts <= int(r["fundingTime"]) <= end_ts
    ]
    out.sort(key=lambda x: x["ts"])
    return out


def compute_trade_costs(direction, entry, risk, fill_ts, exit_ts, funding_events):
    """
    Returns (fee_r, funding_r) — both already converted to R-multiples so
    they can be subtracted/added directly to a trade's raw r_multiple.
    Positive funding_r means the trade received funding; negative means
    it paid. fee_r is always a cost (positive number to subtract).
    """
    fee_price = entry * (ENTRY_FEE_PCT + EXIT_FEE_PCT) / 100
    fee_r = fee_price / risk if risk else 0.0

    funding_r = 0.0
    for ev in funding_events:
        if fill_ts < ev["ts"] <= exit_ts:
            cost_price = entry * ev["rate"]  # positive rate: longs pay shorts
            if direction == "long":
                funding_r -= cost_price / risk
            else:
                funding_r += cost_price / risk

    return fee_r, funding_r


def resample_htf(candles_1h, group_ms=HTF_GROUP_MS):
    """
    Aggregate 1H candles into HTF candles anchored to real HTF boundaries
    (audit fix). Each 1H candle is assigned to the bucket
    ts - (ts % group_ms), which matches how OKX's own 4H candles are
    aligned. The trailing bucket is dropped unless it contains a full
    group of 1H candles, so — like the live bot after its confirmed-
    candle fix — only *completed* HTF bars feed the trend filter.
    Uses only past data; no lookahead.
    """
    if not candles_1h:
        return []

    buckets = []
    current_key = None
    for c in candles_1h:
        key = c["ts"] - (c["ts"] % group_ms)
        if key != current_key:
            buckets.append({
                "ts": key,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "vol": c["vol"],
                "count": 1,
            })
            current_key = key
        else:
            b = buckets[-1]
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["vol"] += c["vol"]
            b["count"] += 1

    group = group_ms // (3600 * 1000)
    # Drop the trailing (still-forming) bucket; keep a partial *first*
    # bucket out too, since it also isn't a true full HTF candle.
    complete = [b for b in buckets[:-1] if b["count"] == group]
    if buckets and buckets[-1]["count"] == group:
        complete.append(buckets[-1])
    return [{k: b[k] for k in ("ts", "open", "high", "low", "close", "vol")} for b in complete]


def evaluate_signal_at(candles, i, previous_raw_direction=None):
    """Run the exact same logic as build_report(), but using only candles[:i+1]."""
    window = candles[:i + 1]
    if len(window) < WARMUP_CANDLES:
        return None

    closes = [c["close"] for c in window]
    price = closes[-1]
    r = bot.rsi(closes)
    _, _, hist = bot.macd(closes)
    ema20 = bot.ema_last(closes, 20)
    ema50 = bot.ema_last(closes, 50)
    supports, resistances = bot.support_resistance(window)
    atr_value = bot.atr(window)
    adx_value = bot.adx(window)

    score = 50
    if r is not None:
        score += (r - 50) * 0.4
    score += 15 if ema20 > ema50 else -15
    score += 10 if hist > 0 else -10

    vol_ratio = bot.volume_ratio(window)
    if vol_ratio is not None:
        deviation = score - 50
        if vol_ratio >= bot.VOLUME_CONFIRM_RATIO:
            deviation *= 1.15
        elif vol_ratio <= bot.VOLUME_LOW_RATIO:
            deviation *= 0.7
        score = 50 + deviation

    score = max(5, min(95, round(score)))

    htf_candles = resample_htf(window)
    htf_trend = None
    if len(htf_candles) >= 20:
        htf_closes = [c["close"] for c in htf_candles]
        e20 = bot.ema_last(htf_closes, 20)
        e50 = bot.ema_last(htf_closes, 50)
        htf_trend = "bullish" if e20 > e50 else "bearish"

    plan = bot.suggest_trade_plan(price, score, atr_value, supports, resistances, htf_trend, adx_value, previous_raw_direction)
    return plan


def simulate_trade(candles, signal_index, plan, funding_events=None):
    """
    Entry is a pullback level, not the current price, so it's treated as a
    pending limit order: we first wait for price to actually reach entry
    (within ENTRY_WAIT_CANDLES) before any risk is considered "live".
    If it never fills, the signal is discarded — not counted as a trade.
    Once filled, walk forward until stop or target is hit, or
    MAX_HOLD_CANDLES elapses (timeout, marked-to-market at last close).

    Returns (outcome, net_r_multiple, exit_ts, cost_breakdown) where
    cost_breakdown = {"gross_r", "fee_r", "funding_r"}.
    """
    funding_events = funding_events or []
    direction = plan["direction"]
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    risk = abs(entry - stop)

    fill_index = None
    for j in range(signal_index + 1, min(signal_index + 1 + ENTRY_WAIT_CANDLES, len(candles))):
        c = candles[j]
        if direction == "long" and c["low"] <= entry:
            fill_index = j
            break
        if direction == "short" and c["high"] >= entry:
            fill_index = j
            break

    if fill_index is None:
        return "no_fill", 0.0, None, None

    # Re-check the thesis at fill time. A pullback entry can take a while
    # to actually get touched — if the setup has flipped by then, the
    # original plan is stale and shouldn't be blindly executed. We pass
    # the original direction as "previous_raw_direction" here so the
    # persistence gate doesn't spuriously block this re-check — we only
    # care whether the raw signal has actually flipped, not re-requiring
    # a fresh 2-hour confirmation at fill time.
    fresh_plan = evaluate_signal_at(candles, fill_index, previous_raw_direction=direction)
    if not fresh_plan or fresh_plan["direction"] != direction:
        return "invalidated", 0.0, None, None

    fill_ts = candles[fill_index]["ts"]

    def finalize(outcome, gross_r, exit_ts):
        fee_r, funding_r = compute_trade_costs(direction, entry, risk, fill_ts, exit_ts, funding_events)
        net_r = gross_r - fee_r + funding_r
        return outcome, net_r, exit_ts, {"gross_r": gross_r, "fee_r": fee_r, "funding_r": funding_r}

    for j in range(fill_index, min(fill_index + MAX_HOLD_CANDLES, len(candles))):
        c = candles[j]
        if direction == "long":
            hit_stop = c["low"] <= stop
            hit_target = c["high"] >= target
        else:
            hit_stop = c["high"] >= stop
            hit_target = c["low"] <= target

        # If both could have been touched in the same candle, assume the
        # worse outcome (stop) hits first — conservative assumption.
        if hit_stop:
            return finalize("loss", -1.0, candles[j]["ts"])
        if hit_target:
            r_multiple = (target - entry) / risk if direction == "long" else (entry - target) / risk
            return finalize("win", r_multiple, candles[j]["ts"])

    # Timed out — mark to market
    last_index = min(fill_index + MAX_HOLD_CANDLES, len(candles) - 1)
    last_close = candles[last_index]["close"]
    r_multiple = (last_close - entry) / risk if direction == "long" else (entry - last_close) / risk
    return finalize("timeout", r_multiple, candles[last_index]["ts"])


def run_backtest(candles, funding_events=None):
    trades = []
    no_fill_count = 0
    invalidated_count = 0
    busy_until = -1  # don't take overlapping trades — one position at a time
    previous_raw_direction = None  # tracked every hour, matching the live bot's persistence gate

    for i in range(WARMUP_CANDLES, len(candles) - 1):
        plan = evaluate_signal_at(candles, i, previous_raw_direction)
        previous_raw_direction = plan.get("raw_direction") if plan else None

        if i <= busy_until:
            continue
        if not plan or not plan["direction"]:
            continue

        outcome, r_multiple, exit_ts, costs = simulate_trade(candles, i, plan, funding_events)
        if outcome == "no_fill":
            no_fill_count += 1
            continue
        if outcome == "invalidated":
            invalidated_count += 1
            continue

        trades.append({
            "entry_ts": candles[i]["ts"],
            "direction": plan["direction"],
            "entry": plan["entry"],
            "stop": plan["stop"],
            "target": plan["target"],
            "rr_planned": plan["rr"],
            "outcome": outcome,
            "gross_r": costs["gross_r"],
            "fee_r": costs["fee_r"],
            "funding_r": costs["funding_r"],
            "r_multiple": r_multiple,  # net of fees and funding
            "exit_ts": exit_ts,
        })
        # find index of exit_ts to know when we're free to trade again
        exit_index = next((k for k in range(i + 1, len(candles)) if candles[k]["ts"] == exit_ts), i + ENTRY_WAIT_CANDLES + MAX_HOLD_CANDLES)
        busy_until = exit_index

    return trades, no_fill_count, invalidated_count


def summarize(trades, no_fill_count=0, invalidated_count=0):
    total_signals = len(trades) + no_fill_count + invalidated_count
    if total_signals:
        fill_rate = len(trades) / total_signals * 100
        print(f"Signals generated: {total_signals}  (filled & valid: {len(trades)}, never reached entry: {no_fill_count}, invalidated before fill: {invalidated_count}, fill rate: {fill_rate:.0f}%)")

    if not trades:
        print("No filled trades in this window — try a longer period, a wider ENTRY_WAIT_CANDLES, or looser thresholds.")
        return None

    wins = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    timeouts = [t for t in trades if t["outcome"] == "timeout"]
    decided = wins + losses  # excludes timeouts from win-rate math

    win_rate = len(wins) / len(decided) * 100 if decided else 0.0
    avg_r = sum(t["r_multiple"] for t in trades) / len(trades)
    avg_gross_r = sum(t["gross_r"] for t in trades) / len(trades)
    avg_fee_r = sum(t["fee_r"] for t in trades) / len(trades)
    avg_funding_r = sum(t["funding_r"] for t in trades) / len(trades)
    expectancy = avg_r  # already in R-multiples, 1R = planned risk per trade

    # Equity curve assuming fixed % risk per trade
    equity = [100.0]
    for t in trades:
        equity.append(equity[-1] * (1 + t["r_multiple"] * RISK_PER_TRADE_PCT / 100))

    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100
        max_dd = max(max_dd, dd)

    print(f"Total signals traded: {len(trades)}")
    print(f"  Wins: {len(wins)}   Losses: {len(losses)}   Timeouts: {len(timeouts)}")
    print(f"Win rate (excl. timeouts): {win_rate:.1f}%")
    print(f"Average gross R-multiple (before costs): {avg_gross_r:+.2f}R")
    print(f"  minus avg fee cost: {avg_fee_r:.2f}R")
    print(f"  {'plus' if avg_funding_r >= 0 else 'minus'} avg funding: {avg_funding_r:+.2f}R")
    print(f"Average NET R-multiple per trade (after fees + funding): {avg_r:+.2f}R")
    print(f"Expectancy (net): {expectancy:+.2f}R per trade")
    print(f"Simulated equity (start 100, risking {RISK_PER_TRADE_PCT}%/trade): {equity[-1]:.1f}")
    print(f"Max drawdown: {max_dd:.1f}%")

    return equity


def save_csv(trades, path="trades.csv"):
    if not trades:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        w.writeheader()
        for t in trades:
            row = dict(t)
            row["entry_ts"] = datetime.fromtimestamp(t["entry_ts"] / 1000, tz=timezone.utc).isoformat()
            row["exit_ts"] = datetime.fromtimestamp(t["exit_ts"] / 1000, tz=timezone.utc).isoformat()
            w.writerow(row)
    print(f"Saved trade log: {path}")


def save_equity_chart(equity, path="equity_curve.png"):
    if not equity:
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.figure(figsize=(9, 4.5))
    plt.plot(equity, linewidth=1.5)
    plt.title("Simulated Equity Curve (backtest)")
    plt.xlabel("Trade #")
    plt.ylabel("Equity (start = 100)")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    print(f"Saved equity chart: {path}")


def quick_stats(trades):
    """One-line stats for a subset of trades, used by the split comparison."""
    if not trades:
        return "no trades"
    wins = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    decided = wins + losses
    win_rate = len(wins) / len(decided) * 100 if decided else 0.0
    avg_r = sum(t["r_multiple"] for t in trades) / len(trades)
    return f"{len(trades)} trades, win rate {win_rate:.1f}%, avg {avg_r:+.2f}R/trade"


def print_split_comparison(trades):
    """
    Compare the first half vs second half of the window chronologically.
    If the edge is real, both halves should look broadly similar. If one
    half carries all the profit and the other is flat/negative, the
    aggregate number is likely inflated by a lucky period rather than a
    consistent edge.
    """
    if len(trades) < 10:
        print("\n(Too few trades to split into halves meaningfully — need more data.)")
        return
    mid = len(trades) // 2
    first_half, second_half = trades[:mid], trades[mid:]
    print("\n--- Walk-forward check: first half vs second half of the window ---")
    print(f"First half:  {quick_stats(first_half)}")
    print(f"Second half: {quick_stats(second_half)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inst", default=bot.INST_ID)
    parser.add_argument("--bar", default=bot.BAR)
    parser.add_argument("--months", type=float, default=6.0)
    parser.add_argument("--end-date", default=None,
                         help="Pull data ending at this date instead of now, e.g. 2024-06-01. "
                              "Use this to test an earlier out-of-sample period.")
    args = parser.parse_args()

    bars_per_month = {"1H": 24 * 30, "15m": 24 * 4 * 30, "4H": 6 * 30}
    target_count = int(bars_per_month.get(args.bar, 24 * 30) * args.months) + WARMUP_CANDLES

    end_ts = None
    if args.end_date:
        end_dt = datetime.strptime(args.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_ts = int(end_dt.timestamp() * 1000)

    print(f"Fetching ~{target_count} {args.bar} candles for {args.inst}" + (f" ending {args.end_date}" if args.end_date else "") + " ...")
    candles = fetch_historical_candles(args.inst, args.bar, target_count, end_ts)
    print(f"Got {len(candles)} candles.")

    print("Fetching real historical funding rates ...")
    funding_events = fetch_funding_history(args.inst, candles[0]["ts"], candles[-1]["ts"])
    period_hours = (candles[-1]["ts"] - candles[0]["ts"]) / (3600 * 1000)
    expected_funding_events = period_hours / 8  # funding typically settles ~every 8h
    print(f"Got {len(funding_events)} funding events (expected roughly {expected_funding_events:.0f} for this period).")
    if expected_funding_events > 0 and len(funding_events) < expected_funding_events * 0.5:
        print("WARNING: funding coverage looks incomplete for this period — OKX's API often only serves a")
        print("         limited rolling window of funding history. Results below likely UNDERSTATE real funding")
        print("         costs for the earlier part of this window. Treat the net R-multiple with extra caution.")
    print("Running backtest ...")

    trades, no_fill_count, invalidated_count = run_backtest(candles, funding_events)
    equity = summarize(trades, no_fill_count, invalidated_count)
    print_split_comparison(trades)
    save_csv(trades)
    save_equity_chart(equity)


if __name__ == "__main__":
    main()
