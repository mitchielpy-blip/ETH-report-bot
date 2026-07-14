"""
ETH Report Bot — Bias-Score Calibration
-----------------------------------------
Answers the one question everything downstream assumes but nothing has ever
checked: does compute_bias_score actually forecast market direction?

The whole pipeline treats score >= LONG_SCORE_MIN as "more likely up" and
score <= SHORT_SCORE_MAX as "more likely down". This script tests that
directly. It walks the candles forward (no lookahead — the score at bar i
uses only candles[:i+1], exactly as the live bot and backtest compute it),
pairs each bar's score with its realized forward return over the next
`horizon` bars, and buckets the results by score. If the score forecasts
direction, the up-rate and mean forward return should rise monotonically
across score buckets, and the live thresholds should carve out buckets that
genuinely beat / trail the base rate.

Crucially it reports every bucket's up-rate as LIFT over the sample's base
up-rate. Crypto trends, so in an up-year every bucket shows >50% up by
drift alone — only lift (bucket up-rate minus base up-rate) tells you the
score is adding directional information rather than just riding the tide.

This does NOT change eth_report_bot.py or its live behavior. It only
measures the score the bot already produces.

Usage:
    pip install -r requirements.txt
    python score_calibration.py                                  # ETH, 12mo, horizons 6/12/24
    python score_calibration.py --inst SOL-USDT-SWAP --months 12
    python score_calibration.py --horizons 12 --end-date 2024-06-01   # out-of-sample

Output:
    Per-horizon console tables (n, up-rate, lift, mean/median forward return,
    plus a first-half/second-half up-rate split to check the relationship is
    stable, not carried by one stretch of the window).
    score_calibration.csv — one row per (horizon, bucket) for your records.

Reading the results:
  - LIFT is the number that matters, not raw up-rate. A high-score bucket
    with +0.0 lift forecasts nothing; it's just the market's drift.
  - You want the up-rate / lift to climb monotonically from the low-score
    buckets to the high-score ones, the LONG zone (score >= LONG_SCORE_MIN)
    to sit clearly above the base rate, and the SHORT zone
    (score <= SHORT_SCORE_MAX) clearly below it.
  - Check the H1/H2 columns: a relationship that only holds in one half of
    the window is not one to trust.
  - If the curve is flat, the score is not forecasting direction, and no
    downstream gate can manufacture an edge from it — that's a finding, and
    it points at rebuilding the score itself rather than adding filters.
"""

import argparse
import csv

import eth_report_bot as bot
import backtest as bt

WARMUP = 60   # bars before the first score (EMA50 + MACD(26) + volume(20) all meaningful)

# Right-exclusive score-bucket edges. Boundaries are placed at the live
# thresholds so the SHORT zone (score <= 45) is exactly the first two buckets
# and the LONG zone (score >= 62) is exactly the last two — the display curve
# and the threshold aggregates line up. (Uses the module defaults 45 / 62;
# the aggregates below are computed from the live values regardless.)
BUCKET_EDGES = [5, 35, 46, 55, 62, 75, 96]


def bucket_label(score):
    for lo, hi in zip(BUCKET_EDGES, BUCKET_EDGES[1:]):
        if lo <= score < hi:
            return f"{lo}-{hi - 1}"
    return "oob"


def forward_return(candles, i, horizon):
    c0 = candles[i]["close"]
    c1 = candles[i + horizon]["close"]
    return (c1 - c0) / c0 if c0 else None


def collect_observations(candles, horizon, warmup=WARMUP):
    """(score, forward_return) for every bar that has both a meaningful score
    and a full `horizon` of future bars. Time-ordered, no lookahead."""
    obs = []
    last = len(candles) - horizon
    for i in range(warmup, last):
        score = bot.compute_bias_score(candles[:i + 1])
        fr = forward_return(candles, i, horizon)
        if fr is not None:
            obs.append((score, fr))
    return obs


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _up_rate(frs):
    return 100.0 * sum(1 for f in frs if f > 0) / len(frs) if frs else 0.0


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return 0.0
    return cov / (vx * vy) ** 0.5


def summarize(obs, long_thr=None, short_thr=None):
    """Turn (score, fwd_return) observations into a calibration summary:
    per-bucket up-rate/lift/returns with a first-half/second-half split, plus
    the LONG/SHORT threshold-zone aggregates and the overall score<->return
    correlation. Pure and network-free so it can be unit-tested directly."""
    long_thr = bot.LONG_SCORE_MIN if long_thr is None else long_thr
    short_thr = bot.SHORT_SCORE_MAX if short_thr is None else short_thr

    all_fr = [f for _, f in obs]
    base_up = _up_rate(all_fr)
    mid = len(obs) // 2  # time-ordered, so first half = older
    first_half, second_half = obs[:mid], obs[mid:]

    def zone(pred):
        frs = [f for s, f in obs if pred(s)]
        h1 = [f for s, f in first_half if pred(s)]
        h2 = [f for s, f in second_half if pred(s)]
        return {
            "n": len(frs),
            "up_rate": _up_rate(frs),
            "lift": _up_rate(frs) - base_up,
            "mean_fwd_pct": _mean(frs) * 100,
            "median_fwd_pct": _median(frs) * 100,
            "h1_up": _up_rate(h1),
            "h2_up": _up_rate(h2),
        }

    buckets = []
    for lo, hi in zip(BUCKET_EDGES, BUCKET_EDGES[1:]):
        label = f"{lo}-{hi - 1}"
        row = {"bucket": label}
        row.update(zone(lambda s, lo=lo, hi=hi: lo <= s < hi))
        buckets.append(row)

    long_zone = zone(lambda s: s >= long_thr)
    short_zone = zone(lambda s: s <= short_thr)
    neutral_zone = zone(lambda s: short_thr < s < long_thr)

    scores = [s for s, _ in obs]
    corr = _pearson(scores, all_fr)

    return {
        "n": len(obs),
        "base_up": base_up,
        "base_mean_fwd_pct": _mean(all_fr) * 100,
        "corr": corr,
        "buckets": buckets,
        "long_zone": long_zone,
        "short_zone": short_zone,
        "neutral_zone": neutral_zone,
        "long_thr": long_thr,
        "short_thr": short_thr,
    }


def print_report(summary, horizon):
    print(f"\n=== Forward horizon: {horizon} bars ===")
    print(f"Observations: {summary['n']}   "
          f"Base up-rate: {summary['base_up']:.1f}%   "
          f"Base mean fwd return: {summary['base_mean_fwd_pct']:+.3f}%")
    print(f"Score<->forward-return correlation: {summary['corr']:+.3f}")

    header = f"{'bucket':<9} {'n':>5} {'up%':>6} {'lift':>7} {'meanFwd%':>9} {'medFwd%':>8} {'H1up%':>7} {'H2up%':>7}"
    print("\n" + header)
    print("-" * len(header))

    def line(label, z):
        if z["n"] == 0:
            print(f"{label:<9} {0:>5} {'-':>6} {'-':>7} {'-':>9} {'-':>8} {'-':>7} {'-':>7}")
            return
        print(f"{label:<9} {z['n']:>5} {z['up_rate']:>5.1f}% {z['lift']:>+6.1f} "
              f"{z['mean_fwd_pct']:>+9.3f} {z['median_fwd_pct']:>+8.3f} "
              f"{z['h1_up']:>6.1f}% {z['h2_up']:>6.1f}%")

    for b in summary["buckets"]:
        line(b["bucket"], b)

    print("-" * len(header))
    line(f">= {summary['long_thr']:.0f}", summary["long_zone"])
    line("neutral", summary["neutral_zone"])
    line(f"<= {summary['short_thr']:.0f}", summary["short_zone"])
    print(f"\n(LONG zone wants up% clearly ABOVE base {summary['base_up']:.1f}% (positive lift); "
          f"SHORT zone wants it clearly BELOW.)")


def rows_for_csv(summary, horizon):
    def row(label, z):
        return {
            "horizon": horizon, "bucket": label, "n": z["n"],
            "up_rate_pct": round(z["up_rate"], 2), "lift_pts": round(z["lift"], 2),
            "mean_fwd_pct": round(z["mean_fwd_pct"], 4),
            "median_fwd_pct": round(z["median_fwd_pct"], 4),
            "h1_up_pct": round(z["h1_up"], 2), "h2_up_pct": round(z["h2_up"], 2),
        }
    out = [row(b["bucket"], b) for b in summary["buckets"]]
    out.append(row(f"LONG_>={summary['long_thr']:.0f}", summary["long_zone"]))
    out.append(row("NEUTRAL", summary["neutral_zone"]))
    out.append(row(f"SHORT_<={summary['short_thr']:.0f}", summary["short_zone"]))
    return out


def save_csv(all_rows, path="score_calibration.csv"):
    if not all_rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"\nSaved: {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inst", default=bot.INST_ID)
    parser.add_argument("--bar", default=bot.BAR)
    parser.add_argument("--months", type=float, default=12.0)
    parser.add_argument("--end-date", default=None,
                         help="Pull data ending at this date instead of now, e.g. 2024-06-01.")
    parser.add_argument("--horizons", default="6,12,24",
                         help="Comma-separated forward horizons in bars (default 6,12,24).")
    args = parser.parse_args()

    horizons = [int(h.strip()) for h in args.horizons.split(",")]

    target_count = bt.target_count_for(args.bar, args.months)
    end_ts = bt.parse_end_ts(args.end_date)

    print(f"Fetching ~{target_count} {args.bar} candles for {args.inst}"
          + (f" ending {args.end_date}" if args.end_date else "") + " ...")
    candles = bt.fetch_historical_candles(args.inst, args.bar, target_count, end_ts)
    print(f"Got {len(candles)} candles.")
    print(f"Calibrating the bias score against forward returns over horizons {horizons} "
          f"(LONG_SCORE_MIN={bot.LONG_SCORE_MIN:.0f}, SHORT_SCORE_MAX={bot.SHORT_SCORE_MAX:.0f}) ...")

    all_rows = []
    for h in horizons:
        obs = collect_observations(candles, h)
        if not obs:
            print(f"\nHorizon {h}: not enough data.")
            continue
        summary = summarize(obs)
        print_report(summary, h)
        all_rows.extend(rows_for_csv(summary, h))

    save_csv(all_rows)

    print("\nReminder: LIFT (bucket up-rate minus the base up-rate), not raw up-rate, is what "
          "shows the score adding directional information — crypto's drift makes every bucket "
          "look bullish in an up-year. A flat lift curve means the score isn't forecasting "
          "direction, whatever the raw up-rates say. This script does not change "
          "eth_report_bot.py or state.json.")


if __name__ == "__main__":
    main()
