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
import bisect
import csv
import os
import time
import sys
from datetime import datetime, timezone

import requests

import eth_report_bot as bot
from exit_manager import ManagedExit

OKX_BASE = bot.OKX_BASE   # honours the OKX_BASE env override from eth_report_bot
OKX_PROXIES = bot.OKX_PROXIES  # route OKX calls through OKX_PROXY when set (geo-block workaround)
PAGE_LIMIT = 100          # OKX history-candles max per request
MAX_HOLD_CANDLES = 72     # give a filled trade up to 72 bars to hit SL/TP before timing out
ENTRY_WAIT_CANDLES = int(os.environ.get("ENTRY_WAIT_HOURS", 24))   # give a pending pullback order this many bars to actually fill (assumes 1H bars). 24 (was 8): kept equal to the live lifetime constants; longer window validated in/out of sample (see eth_report_bot.py)
WARMUP_CANDLES = 260      # candles needed before the first signal can be evaluated
RISK_PER_TRADE_PCT = 1.0  # for the equity curve simulation only

# Trading cost assumptions — these are approximate OKX USDT-margined perp
# rates for a regular (non-VIP) account. Check your actual fee tier under
# Account -> Fees on OKX and adjust if different.
ENTRY_FEE_PCT = 0.02   # pullback entry is a resting limit order -> maker fee
EXIT_FEE_PCT = 0.05    # stop-loss/take-profit triggers execute as market -> taker fee

# Funding modeling — backtest cost estimate only, NEVER touches live behaviour.
# OKX's public funding-rate-history endpoint only serves a limited recent window
# (a few months), so any window whose older portion predates that retention came
# back with ZERO funding events. Zero funding silently undercharged funding drag,
# and because drag scales with time-in-market it biased comparisons toward
# high-trade-count settings. build_funding_events() fills the un-served older gap
# with modeled events on an 8h grid. The rate is normally ESTIMATED from whatever
# recent funding OKX WILL serve (its mean); real events are always kept as-is and
# only the gap is modeled. Setting ASSUMED_FUNDING_RATE in the env OVERRIDES the
# estimate entirely — use it to stress-test a heavier historical funding regime
# than today's (e.g. ASSUMED_FUNDING_RATE=0.0005 for 0.05%/8h). When it is not
# set, it also serves as the last-resort default if OKX serves no funding at all.
FUNDING_INTERVAL_MS = 8 * 3600 * 1000   # OKX funding settles ~every 8h
_ASSUMED_FUNDING_RATE_ENV = os.environ.get("ASSUMED_FUNDING_RATE")
FUNDING_RATE_OVERRIDDEN = _ASSUMED_FUNDING_RATE_ENV not in (None, "")   # env set => force this rate for modeled events
ASSUMED_FUNDING_RATE = float(_ASSUMED_FUNDING_RATE_ENV) if FUNDING_RATE_OVERRIDDEN else 0.0001  # per-8h rate (~0.01%, a neutral long-run perp average)

HTF_GROUP_HOURS = 4
HTF_GROUP_MS = HTF_GROUP_HOURS * 3600 * 1000

# Approximate candles per month per bar size, used to translate a --months
# argument into a fetch count. Shared by backtest.py, backtest_sweep.py and
# diagnostics.py so they all size their windows identically.
BARS_PER_MONTH = {"1H": 24 * 30, "15m": 24 * 4 * 30, "4H": 6 * 30, "5m": 12 * 24 * 30}

# --- Price-action backtest (STRATEGY=price_action) --------------------------
# The whole strategy is driven from ONE 5M history feed, resampled up to
# 15m/1H/4H (mirroring how the live bot fetches those four timeframes). Bar
# sizes in ms and the base feed size:
PA_BASE_MS = 5 * 60 * 1000
PA_TF_MS = {"15m": 15 * 60 * 1000, "1H": 60 * 60 * 1000, "4H": 4 * 60 * 60 * 1000}
# As-of window sizes handed to the evaluator per timeframe — kept equal to the
# live fetch_timeframes() limits so the backtest sees exactly what live sees.
PA_LIMITS = {"5m": 200, "15m": 120, "1H": 120, "4H": 120}
PA_WARMUP_5M = int(os.environ.get("PA_WARMUP_5M", 1000))     # 5M bars before the first signal (~20 4H bars)
PA_ENTRY_WAIT_5M = int(os.environ.get("PA_ENTRY_WAIT_BARS", 24))  # 5M bars to retest the broken level (~2h)
PA_MAX_HOLD_5M = int(os.environ.get("PA_MAX_HOLD_BARS", 288))     # 5M bars a filled trade may run (~24h)


def target_count_for(bar, months):
    """How many candles to fetch for `months` of `bar`-sized bars, including
    the warmup the first signal needs. Unknown bars fall back to 1H sizing."""
    return int(BARS_PER_MONTH.get(bar, 24 * 30) * months) + WARMUP_CANDLES


def parse_end_ts(end_date):
    """Millisecond UTC timestamp for a 'YYYY-MM-DD' end date, or None."""
    if not end_date:
        return None
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(end_dt.timestamp() * 1000)


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

        resp = requests.get(f"{OKX_BASE}/api/v5/market/history-candles", params=params, timeout=15, proxies=OKX_PROXIES)
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
    return [bot.parse_candle_row(row) for row in all_rows[-target_count:]]


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
        resp = requests.get(f"{OKX_BASE}/api/v5/public/funding-rate-history", params=params, timeout=15, proxies=OKX_PROXIES)
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


def _model_funding_gap(start_ts, end_ts, real, rate):
    """
    Merge real funding events with modeled events on an 8h grid that fill the
    older part of [start_ts, end_ts] OKX won't serve. OKX only keeps recent
    funding, so `real` (when non-empty) covers the newest part of the window;
    the gap to fill is [start_ts, earliest_real_ts). Pure and testable — takes
    the estimated `rate` as an argument. Returns (events, modeled_count).
    """
    gap_end = min((e["ts"] for e in real), default=int(end_ts))
    modeled = []
    if gap_end - int(start_ts) >= FUNDING_INTERVAL_MS:
        first = ((int(start_ts) // FUNDING_INTERVAL_MS) + 1) * FUNDING_INTERVAL_MS
        modeled = [{"ts": t, "rate": rate} for t in range(first, gap_end, FUNDING_INTERVAL_MS)]
    events = sorted(real + modeled, key=lambda e: e["ts"])
    return events, len(modeled)


def estimate_recent_funding_rate(inst_id, in_window_real):
    """
    A per-8h funding rate to price modeled gap events. If ASSUMED_FUNDING_RATE
    is set in the env, that explicit rate wins (stress-test knob). Otherwise
    prefer the mean of the real funding already fetched for this window; if the
    whole window is out of OKX's retention (no in-window real events at all),
    pull whatever funding OKX serves right now and use its mean; failing even
    that, the flat default. Signed, so the modeled drag carries the real
    direction (funding is usually slightly positive => longs pay a little,
    shorts receive a little).
    """
    if FUNDING_RATE_OVERRIDDEN:
        return ASSUMED_FUNDING_RATE          # user forced an explicit rate
    if in_window_real:
        return sum(e["rate"] for e in in_window_real) / len(in_window_real)
    served = fetch_funding_history(inst_id, 0, int(time.time() * 1000))
    if served:
        return sum(e["rate"] for e in served) / len(served)
    return ASSUMED_FUNDING_RATE


def build_funding_events(inst_id, start_ts, end_ts, verbose=True):
    """
    Funding events for [start_ts, end_ts] with the un-served older gap modeled.
    This is the single funding entry point every backtest tool should use (not
    fetch_funding_history directly), so they all charge realistic funding drag
    on out-of-sample windows instead of the silent zero OKX's limited history
    would otherwise leave. Backtest-only cost estimate; live is untouched.
    """
    real = fetch_funding_history(inst_id, start_ts, end_ts)
    gap_end = min((e["ts"] for e in real), default=int(end_ts))
    if gap_end - int(start_ts) < FUNDING_INTERVAL_MS:
        if verbose:
            print(f"Funding: {len(real)} real events, full coverage — nothing modeled.")
        return real

    rate = estimate_recent_funding_rate(inst_id, real)
    events, modeled_count = _model_funding_gap(start_ts, end_ts, real, rate)
    if verbose:
        print(f"Funding: {len(real)} real + {modeled_count} MODELED events at "
              f"{rate:+.4%}/8h over the older part of the window OKX won't serve "
              f"(so funding drag isn't silently zero on out-of-sample runs; "
              f"override the fallback via ASSUMED_FUNDING_RATE).")
    return events


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


def resample_indexed(candles, group_ms, base_ms):
    """
    Aggregate `base_ms`-sized candles into `group_ms` candles anchored to real
    boundaries (each base candle joins the bucket ts - (ts % group_ms), matching
    how OKX aligns its own higher-timeframe candles). Returns a list of
    (close_index, bar) for every *complete* bucket, where close_index is the
    index in `candles` of the bucket's last base candle — so a caller can slice
    "buckets fully closed as of base bar i" with no lookahead. A bucket is
    complete only when it holds all group_ms // base_ms base candles, so a
    still-forming bucket (or one straddling a data gap) is dropped, exactly as
    the live bot only ever sees closed higher-timeframe bars.
    """
    if not candles:
        return []
    buckets = []
    current_key = None
    for idx, c in enumerate(candles):
        key = c["ts"] - (c["ts"] % group_ms)
        if key != current_key:
            buckets.append({
                "ts": key, "open": c["open"], "high": c["high"], "low": c["low"],
                "close": c["close"], "vol": c["vol"], "count": 1, "_ci": idx,
            })
            current_key = key
        else:
            b = buckets[-1]
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["vol"] += c["vol"]
            b["count"] += 1
            b["_ci"] = idx

    group = group_ms // base_ms
    out = []
    for b in buckets:
        if b["count"] == group:
            bar = {k: b[k] for k in ("ts", "open", "high", "low", "close", "vol")}
            out.append((b["_ci"], bar))
    return out


def resample(candles, group_ms, base_ms):
    """Just the completed higher-timeframe bars (no indices) — the general form
    of the old resample_htf, for any base/group size."""
    return [bar for _ci, bar in resample_indexed(candles, group_ms, base_ms)]


def resample_htf(candles_1h, group_ms=HTF_GROUP_MS):
    """Aggregate 1H candles into HTF (default 4H) candles for the indicator
    backtest — a thin wrapper over the general resampler with a 1H base, so the
    indicator path is byte-for-byte unchanged."""
    return resample(candles_1h, group_ms, 3600 * 1000)


def evaluate_signal_at(candles, i, previous_raw_direction=None, require_rr=True):
    """Run the exact same logic as build_report(), but using only candles[:i+1].

    require_rr defaults True for signal generation. The fill-time revalidate
    passes require_rr=False so it matches the live fill checker: a pending order
    already has its R:R locked from signal time, so re-gating on a freshly
    recomputed R:R would discard valid fills (see bot.suggest_trade_plan)."""
    window = candles[:i + 1]
    if len(window) < WARMUP_CANDLES:
        return None

    # The whole plan derivation is delegated to the shared bot.evaluate_plan,
    # so the backtest and the live bot can never derive a plan differently.
    # The only backtest-specific input is the HTF trend, which we compute from
    # resampled 1H bars (the live bot fetches real HTF candles instead) and
    # pass in explicitly — that also guarantees the backtest never hits the
    # network for HTF data.
    htf_trend = bot.htf_trend_from_closes([c["close"] for c in resample_htf(window)])
    return bot.evaluate_plan(window, previous_raw_direction, htf_trend=htf_trend, require_rr=require_rr)


def find_fill_index(candles, signal_index, direction, entry, wait=ENTRY_WAIT_CANDLES):
    """
    Index of the first candle within `wait` bars after the signal where price
    touches `entry`, approaching from whichever side the entry sits on relative
    to the signal close. A pullback entry sits against the signal (long below
    price / short above) and is reached on a retrace; a breakout entry sits in
    the signal's direction and is reached on continuation; a market entry sits
    at the signal price and fills on the next bar. Comparing entry to the signal
    close tells us which way price must move to reach it, so one scan models all
    three ENTRY_MODEs. For the default pullback entry this is identical to the
    original low<=entry (long) / high>=entry (short) test. Returns None if the
    level is never touched inside the window.
    """
    signal_price = candles[signal_index]["close"]
    for j in range(signal_index + 1, min(signal_index + 1 + wait, len(candles))):
        c = candles[j]
        if direction == "long":
            touched = c["low"] <= entry if entry <= signal_price else c["high"] >= entry
        else:
            touched = c["high"] >= entry if entry >= signal_price else c["low"] <= entry
        if touched:
            return j
    return None


def simulate_trade(candles, signal_index, plan, funding_events=None,
                   revalidate=None, wait=ENTRY_WAIT_CANDLES, max_hold=MAX_HOLD_CANDLES):
    """
    Entry is a pullback/retest level, not the current price, so it's treated as
    a pending limit order: we first wait for price to actually reach entry
    (within `wait` bars) before any risk is considered "live". If it never
    fills, the signal is discarded — not counted as a trade. Once filled, walk
    forward until stop or target is hit, or `max_hold` elapses (timeout,
    marked-to-market at last close).

    `revalidate(fill_index) -> plan_or_None` re-checks the thesis at fill time
    (a pullback can take a while to get touched; if the setup has flipped by
    then the original plan is stale). It defaults to the indicator re-check
    (evaluate_signal_at, passing the original direction so the persistence gate
    doesn't spuriously block); the price-action path passes its own multi-TF
    re-check. `wait`/`max_hold` are in units of the base candle so the same
    machinery serves the 1H indicator and the 5M price-action backtests.

    Returns (outcome, net_r_multiple, exit_ts, cost_breakdown, fill_index).
    """
    funding_events = funding_events or []
    direction = plan["direction"]
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    risk = abs(entry - stop)

    fill_index = find_fill_index(candles, signal_index, direction, entry, wait=wait)

    if fill_index is None:
        return "no_fill", 0.0, None, None, None

    if revalidate is None:
        def revalidate(idx):
            # The persistence gate re-checks the NATIVE call's stability, so feed
            # it the plan's raw_direction, not the traded direction. They are equal
            # for the fixed strategy (no behaviour change); under INVERT_SIGNAL the
            # traded direction is flipped while raw_direction stays native, and
            # passing the flipped one here would make the persistence gate reject
            # every fill (raw_direction != flipped) and invalidate the whole run.
            return evaluate_signal_at(candles, idx,
                                      previous_raw_direction=plan.get("raw_direction", direction),
                                      require_rr=False)
    fresh_plan = revalidate(fill_index)
    if not fresh_plan or fresh_plan["direction"] != direction:
        return "invalidated", 0.0, None, None, fill_index

    fill_ts = candles[fill_index]["ts"]

    def finalize(outcome, gross_r, exit_ts):
        fee_r, funding_r = compute_trade_costs(direction, entry, risk, fill_ts, exit_ts, funding_events)
        net_r = gross_r - fee_r + funding_r
        return outcome, net_r, exit_ts, {"gross_r": gross_r, "fee_r": fee_r, "funding_r": funding_r}, fill_index

    # Exit management lives in one shared, pure stepper (exit_manager.ManagedExit)
    # so the backtest and a future live position-manager decide exits identically.
    # With EXIT_MODEL=fixed (the live default) the stop never moves and outcomes
    # are byte-identical to the old inline fixed SL/TP loop; EXIT_MODEL=breakeven
    # slides the stop to entry once the trade has gone BREAKEVEN_AT_R in favour.
    exit_mgr = ManagedExit(direction, entry, stop, target,
                           model=bot.EXIT_MODEL, be_at_r=bot.BREAKEVEN_AT_R,
                           be_buffer_r=bot.BREAKEVEN_BUFFER_R,
                           trail_at_r=bot.TRAIL_AT_R, trail_distance_r=bot.TRAIL_DISTANCE_R,
                           honor_target=(bot.TRAIL_HONOR_TARGET if bot.EXIT_MODEL == "trailing" else None))
    for j in range(fill_index, min(fill_index + max_hold, len(candles))):
        c = candles[j]
        outcome, exit_price = exit_mgr.on_bar(c["high"], c["low"], c["close"])
        if outcome is not None:
            # gross R from the actual exit price: a stop at the original level is
            # exactly -1R, a breakeven stop at entry is ~0R, the target is +RR.
            gross_r = ((exit_price - entry) / risk if direction == "long"
                       else (entry - exit_price) / risk)
            return finalize(outcome, gross_r, candles[j]["ts"])

    # Timed out — mark to market
    last_index = min(fill_index + max_hold, len(candles) - 1)
    last_close = candles[last_index]["close"]
    r_multiple = (last_close - entry) / risk if direction == "long" else (entry - last_close) / risk
    return finalize("timeout", r_multiple, candles[last_index]["ts"])


def walk_forward(candles, funding_events=None):
    """
    The single walk-forward pass shared by the plain backtest and the
    diagnostics run. Steps through the candles once, applying the live
    bot's persistence gate and the one-position-at-a-time busy rule, and
    yields one event dict per signal that isn't blocked by an open trade:

        {"signal_index", "plan", "outcome", "r_multiple", "exit_ts",
         "costs", "fill_index", "exit_index"}

    Events are yielded for "no_fill" and "invalidated" signals too (so
    callers can count them); those carry exit_index=None and never advance
    the busy gate. For a filled trade, exit_index is the candle the trade
    closed on, so callers get fill delay and hold time without re-scanning.
    """
    busy_until = -1  # don't take overlapping trades — one position at a time
    previous_raw_direction = None  # tracked every hour, matching the live bot's persistence gate

    for i in range(WARMUP_CANDLES, len(candles) - 1):
        plan = evaluate_signal_at(candles, i, previous_raw_direction)
        previous_raw_direction = plan.get("raw_direction") if plan else None

        if i <= busy_until:
            continue
        if not plan or not plan["direction"]:
            continue

        outcome, r_multiple, exit_ts, costs, fill_index = simulate_trade(candles, i, plan, funding_events)

        if outcome in ("no_fill", "invalidated"):
            yield {
                "signal_index": i, "plan": plan, "outcome": outcome,
                "r_multiple": r_multiple, "exit_ts": exit_ts, "costs": costs,
                "fill_index": fill_index, "exit_index": None,
            }
            continue

        # find index of exit_ts to know when we're free to trade again
        exit_index = next((k for k in range(i + 1, len(candles)) if candles[k]["ts"] == exit_ts),
                          i + ENTRY_WAIT_CANDLES + MAX_HOLD_CANDLES)
        busy_until = exit_index
        yield {
            "signal_index": i, "plan": plan, "outcome": outcome,
            "r_multiple": r_multiple, "exit_ts": exit_ts, "costs": costs,
            "fill_index": fill_index, "exit_index": exit_index,
        }


def walk_forward_pa(candles_5m, funding_events=None):
    """
    Price-action walk-forward. Driven from a single 5M feed: the 15m/1H/4H
    series are resampled up once, then at each 5M bar the evaluator is handed
    the *last N completed* bars of each timeframe — exactly the windows the live
    fetch_timeframes() would return — so the backtest sees what live sees, with
    no lookahead (a higher bar is included only once its close index <= i).

    Yields the same event dicts as walk_forward(), so run_backtest/summarize
    consume both identically. The busy gate keeps one position at a time, and a
    fired zone is remembered (previous_state) so the same setup doesn't re-fire
    every 5M bar — mirroring the live bot's persisted state.
    """
    # Precompute the higher-TF bars once, each tagged with the 5M index it
    # closes on, so an as-of slice is an O(log n) bisect rather than a rescan.
    indexed = {tf: resample_indexed(candles_5m, PA_TF_MS[tf], PA_BASE_MS) for tf in PA_TF_MS}
    close_idx = {tf: [ci for ci, _ in indexed[tf]] for tf in indexed}
    bars = {tf: [b for _, b in indexed[tf]] for tf in indexed}

    def bundle_at(i):
        b = {"5m": candles_5m[max(0, i - PA_LIMITS["5m"] + 1): i + 1]}
        for tf in PA_TF_MS:
            hi = bisect.bisect_right(close_idx[tf], i)
            lo = max(0, hi - PA_LIMITS[tf])
            b[tf] = bars[tf][lo:hi]
        return b

    def evaluate_at(i, prev_state):
        if i < PA_WARMUP_5M:
            return None
        return bot.evaluate_plan(None, candles_by_tf=bundle_at(i), previous_state=prev_state)

    busy_until = -1
    previous_state = {}  # {"zone_id", "direction"} of the last fired setup — dedupe key

    for i in range(PA_WARMUP_5M, len(candles_5m) - 1):
        plan = evaluate_at(i, previous_state)
        if plan and plan.get("direction"):
            previous_state = {"zone_id": plan.get("zone_id"), "direction": plan["direction"]}

        if i <= busy_until:
            continue
        if not plan or not plan["direction"]:
            continue

        # Fill-time re-check uses the retest-aware guard (4H trend still
        # agrees), NOT the full 4-step chain — otherwise the retest that fills
        # the order (no current BOS) would be invalidated every time. Same
        # function the live fill_checker uses, so backtest and live agree.
        outcome, r_multiple, exit_ts, costs, fill_index = simulate_trade(
            candles_5m, i, plan, funding_events,
            revalidate=lambda idx: bot._price_action_revalidate(bundle_at(idx), plan["direction"]),
            wait=PA_ENTRY_WAIT_5M, max_hold=PA_MAX_HOLD_5M)

        if outcome in ("no_fill", "invalidated"):
            yield {
                "signal_index": i, "plan": plan, "outcome": outcome,
                "r_multiple": r_multiple, "exit_ts": exit_ts, "costs": costs,
                "fill_index": fill_index, "exit_index": None,
            }
            continue

        exit_index = next((k for k in range(i + 1, len(candles_5m)) if candles_5m[k]["ts"] == exit_ts),
                          i + PA_ENTRY_WAIT_5M + PA_MAX_HOLD_5M)
        busy_until = exit_index
        yield {
            "signal_index": i, "plan": plan, "outcome": outcome,
            "r_multiple": r_multiple, "exit_ts": exit_ts, "costs": costs,
            "fill_index": fill_index, "exit_index": exit_index,
        }


def run_backtest(candles, funding_events=None, walker=walk_forward):
    trades = []
    no_fill_count = 0
    invalidated_count = 0

    for ev in walker(candles, funding_events):
        if ev["outcome"] == "no_fill":
            no_fill_count += 1
            continue
        if ev["outcome"] == "invalidated":
            invalidated_count += 1
            continue

        plan, costs = ev["plan"], ev["costs"]
        trades.append({
            "entry_ts": candles[ev["signal_index"]]["ts"],
            "direction": plan["direction"],
            "entry": plan["entry"],
            "stop": plan["stop"],
            "target": plan["target"],
            "rr_planned": plan["rr"],
            "outcome": ev["outcome"],
            "gross_r": costs["gross_r"],
            "fee_r": costs["fee_r"],
            "funding_r": costs["funding_r"],
            "r_multiple": ev["r_multiple"],  # net of fees and funding
            "exit_ts": ev["exit_ts"],
        })

    return trades, no_fill_count, invalidated_count


def win_rate_and_avg_r(trades, r_key="r_multiple"):
    """
    (win_rate_pct, avg_r) over `trades`. Win rate excludes timeouts
    (only decided win/loss trades count); average R is over every trade,
    read from `r_key`. Empty input returns (0.0, 0.0). Shared by every
    summary path so they compute these two headline numbers identically.
    """
    if not trades:
        return 0.0, 0.0
    wins = sum(1 for t in trades if t["outcome"] == "win")
    losses = sum(1 for t in trades if t["outcome"] == "loss")
    decided = wins + losses
    win_rate = wins / decided * 100 if decided else 0.0
    avg_r = sum(t[r_key] for t in trades) / len(trades)
    return win_rate, avg_r


def equity_and_drawdown(trades, risk_pct=RISK_PER_TRADE_PCT, r_key="r_multiple"):
    """
    Simulated equity curve (starting at 100, risking `risk_pct`% per trade)
    and the max drawdown % along it. Returns (equity_list, max_dd_pct).
    Shared by summarize() and the sweep so both simulate equity identically.
    """
    equity = [100.0]
    for t in trades:
        equity.append(equity[-1] * (1 + t[r_key] * risk_pct / 100))
    peak, max_dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)
    return equity, max_dd


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
    # "breakeven" only appears under EXIT_MODEL=breakeven: a trade whose stop was
    # moved to entry and then tagged. Like timeouts it's neither a clean win nor
    # loss, so it's excluded from the win-rate denominator (win_rate_and_avg_r
    # already counts only wins/losses) but its ~0R still lands in expectancy.
    breakevens = [t for t in trades if t["outcome"] == "breakeven"]

    win_rate, avg_r = win_rate_and_avg_r(trades)
    avg_gross_r = sum(t["gross_r"] for t in trades) / len(trades)
    avg_fee_r = sum(t["fee_r"] for t in trades) / len(trades)
    avg_funding_r = sum(t["funding_r"] for t in trades) / len(trades)
    expectancy = avg_r  # already in R-multiples, 1R = planned risk per trade

    equity, max_dd = equity_and_drawdown(trades)

    print(f"Total signals traded: {len(trades)}")
    print(f"  Wins: {len(wins)}   Losses: {len(losses)}   Breakevens: {len(breakevens)}   Timeouts: {len(timeouts)}")
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
    win_rate, avg_r = win_rate_and_avg_r(trades)
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
    price_action = bot.STRATEGY == "price_action"

    parser = argparse.ArgumentParser()
    parser.add_argument("--inst", default=bot.INST_ID)
    # price_action is driven from a single 5M feed (resampled up to 15m/1H/4H),
    # so its base bar is fixed at 5m; the indicator backtest keeps --bar.
    parser.add_argument("--bar", default="5m" if price_action else bot.BAR)
    parser.add_argument("--months", type=float, default=6.0)
    parser.add_argument("--end-date", default=None,
                         help="Pull data ending at this date instead of now, e.g. 2024-06-01. "
                              "Use this to test an earlier out-of-sample period.")
    args = parser.parse_args()

    if price_action:
        args.bar = "5m"  # base feed is fixed for the multi-TF strategy
        walker = walk_forward_pa
        target_count = int(BARS_PER_MONTH["5m"] * args.months) + PA_WARMUP_5M
        print(f"STRATEGY=price_action — driving 4H/1H/15M/5M from a single 5M feed.")
    else:
        walker = walk_forward
        target_count = target_count_for(args.bar, args.months)
        # Echo the research knobs so every run's log is self-describing (which
        # matters when sweeping the multipliers across many otherwise-identical
        # dispatches). Live defaults: sl=1.5, pullback=0.7, exit=fixed, invert off.
        print(f"CONFIG: entry_mode={bot.ENTRY_MODE} atr_sl_mult={bot.ATR_SL_MULT} "
              f"pullback_atr_mult={bot.PULLBACK_ATR_MULT} min_rr={bot.MIN_RR} "
              f"adx_min={bot.ADX_MIN} skip_sessions='{bot.SKIP_SESSIONS}' "
              f"entry_wait={ENTRY_WAIT_CANDLES} "
              f"exit_model={bot.EXIT_MODEL} invert_signal={bot.INVERT_SIGNAL} "
              f"disable_htf_filter={bot.DISABLE_HTF_FILTER}")

    end_ts = parse_end_ts(args.end_date)

    print(f"Fetching ~{target_count} {args.bar} candles for {args.inst}" + (f" ending {args.end_date}" if args.end_date else "") + " ...")
    candles = fetch_historical_candles(args.inst, args.bar, target_count, end_ts)
    print(f"Got {len(candles)} candles.")

    print("Building funding series (real where OKX serves it, modeled for the older gap) ...")
    funding_events = build_funding_events(args.inst, candles[0]["ts"], candles[-1]["ts"])
    print("Running backtest ...")

    trades, no_fill_count, invalidated_count = run_backtest(candles, funding_events, walker=walker)
    equity = summarize(trades, no_fill_count, invalidated_count)
    print_split_comparison(trades)
    save_csv(trades)
    save_equity_chart(equity)


if __name__ == "__main__":
    main()
