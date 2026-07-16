"""
Price-action strategy core (STRATEGY=price_action)
--------------------------------------------------
A mechanical translation of a 4-timeframe Smart-Money / price-action entry
system into the same "candles -> plan" contract the rest of the bot already
speaks. It answers four questions before proposing a trade:

  1. Trend (4H)        — higher-highs & higher-lows -> longs only; lower-highs
                         & lower-lows -> shorts only; anything else -> stand
                         aside (ranging).
  2. Zone (1H)         — the last swing point that price left *aggressively*
                         (an impulse >= IMPULSE_ATR_MULT x ATR within
                         IMPULSE_MAX_BARS bars). That candle's range is the
                         "where big money entered" reversal zone.
  3. Reaction (15M)    — wait for price to tag that zone and reject it (a wick
                         into the zone, body closing back out). No reaction =
                         no trade.
  4. Confirmation (5M) — wait for a break of the opposite side's structure: for
                         a long, a 5M close above the last lower-high; for a
                         short, a close below the last higher-low.

Every function here is PURE — it takes candle lists (oldest -> newest,
completed bars only) and returns plain values. No network, no os.environ, no
global state — exactly like compute_bias_score in eth_report_bot.py — so the
live bot, fill_checker and the backtest can all drive it and can never derive a
plan differently. All tunables are explicit keyword arguments with defaults;
eth_report_bot passes its env-configured values in.

The orchestrator returns the SAME plan-dict shape as
eth_report_bot.suggest_trade_plan:
    {"direction", "entry", "stop", "target", "rr", "raw_direction", ...}
or {"direction": None, "reason": ..., "raw_direction": ...} when no setup
clears every gate — so should_post/log_signal/save_state/fill_checker keep
working unchanged. It additionally carries "zone_id" (the origin candle's
timestamp) so the caller can avoid re-firing the same setup every run.
"""

import eth_report_bot as bot  # for the shared, already-tested atr() helper


def detect_swings(candles, left=2, right=2):
    """
    Fractal pivot detection. Returns a chronological list of swing points:
        [{"index", "ts", "price", "type": "high"|"low"}, ...]

    A candle i is a swing high if its high strictly exceeds the `left` highs
    before it and the `right` highs after it (symmetric for a swing low). The
    `right` confirmation bars mean a pivot is only ever recognised `right` bars
    after it forms, so scanning a completed-candle window here never peeks at a
    bar that hadn't closed at decision time — no lookahead.
    """
    swings = []
    n = len(candles)
    for i in range(left, n - right):
        hi = candles[i]["high"]
        lo = candles[i]["low"]
        is_high = (
            all(hi > candles[j]["high"] for j in range(i - left, i))
            and all(hi > candles[j]["high"] for j in range(i + 1, i + right + 1))
        )
        if is_high:
            swings.append({"index": i, "ts": candles[i]["ts"], "price": hi, "type": "high"})
            continue
        is_low = (
            all(lo < candles[j]["low"] for j in range(i - left, i))
            and all(lo < candles[j]["low"] for j in range(i + 1, i + right + 1))
        )
        if is_low:
            swings.append({"index": i, "ts": candles[i]["ts"], "price": lo, "type": "low"})
    return swings


def swing_trend(candles, left=2, right=2):
    """
    Classify structure via the last two swing highs and last two swing lows:
      * 'bullish'  — higher high AND higher low (HH & HL)
      * 'bearish'  — lower high  AND lower low  (LH & LL)
      * None       — anything else (ranging / not enough structure)
    This is Step 1, run on the 4H series.
    """
    swings = detect_swings(candles, left, right)
    highs = [s["price"] for s in swings if s["type"] == "high"]
    lows = [s["price"] for s in swings if s["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh = highs[-1] > highs[-2]
    hl = lows[-1] > lows[-2]
    if hh and hl:
        return "bullish"
    lh = highs[-1] < highs[-2]
    ll = lows[-1] < lows[-2]
    if lh and ll:
        return "bearish"
    return None


def find_reversal_zone(candles, direction, atr_value, left=2, right=2,
                       impulse_atr_mult=2.0, impulse_max_bars=5):
    """
    Step 2 (1H): the most recent swing point that price left aggressively.

    For a long we look at swing *lows*; the impulse is the highest high reached
    within `impulse_max_bars` bars after the pivot, measured from the pivot low.
    If that impulse is >= impulse_atr_mult x ATR the departure counts as
    "aggressive" and the pivot candle's full range becomes the demand zone.
    Symmetric for a short (swing highs -> supply zone).

    Returns (zone_low, zone_high, zone_id) — zone_id is the origin candle's
    timestamp — or None if no qualifying zone exists. Scans most-recent-first
    so the freshest untested zone wins.
    """
    if atr_value <= 0:
        return None
    swings = detect_swings(candles, left, right)
    want = "low" if direction == "long" else "high"
    pivots = [s for s in swings if s["type"] == want]

    for pivot in reversed(pivots):
        p = pivot["index"]
        end = min(p + impulse_max_bars, len(candles) - 1)
        if end <= p:
            continue
        legs = candles[p + 1:end + 1]
        if not legs:
            continue
        if direction == "long":
            impulse = max(c["high"] for c in legs) - candles[p]["low"]
        else:
            impulse = candles[p]["high"] - min(c["low"] for c in legs)
        if impulse >= impulse_atr_mult * atr_value:
            return candles[p]["low"], candles[p]["high"], candles[p]["ts"]
    return None


def detect_rejection(candles, zone, direction, lookback=8, wick_ratio=0.5):
    """
    Step 3 (15M): has price tagged the zone and rejected it recently?

    For a long/demand zone we want a candle in the last `lookback` bars whose
    low dips into the zone but whose body closes back *above* the zone, with the
    lower wick making up at least `wick_ratio` of the candle's range (a genuine
    rejection, not a slow bleed through). Symmetric for a short/supply zone.
    Returns True/False.
    """
    zone_low, zone_high = zone[0], zone[1]
    for c in candles[-lookback:]:
        rng = c["high"] - c["low"]
        if rng <= 0:
            continue
        if direction == "long":
            tagged = c["low"] <= zone_high
            closed_out = c["close"] > zone_high
            wick = min(c["open"], c["close"]) - c["low"]
        else:
            tagged = c["high"] >= zone_low
            closed_out = c["close"] < zone_low
            wick = c["high"] - max(c["open"], c["close"])
        if tagged and closed_out and (wick / rng) >= wick_ratio:
            return True
    return False


def detect_bos(candles, direction, left=1, right=1):
    """
    Step 4 (5M): break of the opposite side's structure.

    For a long, find the most recent swing high (the last lower-high of the
    pullback) and require the latest completed 5M candle to *close* above it —
    buyers have taken control. For a short, require a close below the most
    recent swing low. Returns the broken structure level (a price) or None.
    """
    swings = detect_swings(candles, left, right)
    want = "high" if direction == "long" else "low"
    levels = [s for s in swings if s["type"] == want]
    if not levels:
        return None
    level = levels[-1]["price"]
    last_close = candles[-1]["close"]
    if direction == "long" and last_close > level:
        return level
    if direction == "short" and last_close < level:
        return level
    return None


def _nearest_target(candles_1h, direction, entry, left=2, right=2):
    """Next opposing 1H swing ahead of entry (resistance for a long, support for
    a short), or None if there's no structure to aim at."""
    swings = detect_swings(candles_1h, left, right)
    if direction == "long":
        ahead = [s["price"] for s in swings if s["type"] == "high" and s["price"] > entry]
        return min(ahead) if ahead else None
    ahead = [s["price"] for s in swings if s["type"] == "low" and s["price"] < entry]
    return max(ahead) if ahead else None


def _no_trade(reason, raw_direction=None, zone_id=None, suppress_post=False):
    # suppress_post marks a "no news" no-trade (e.g. a zone we've already
    # signalled) so the caller can skip re-posting it every run.
    return {"direction": None, "reason": reason, "raw_direction": raw_direction,
            "zone_id": zone_id, "suppress_post": suppress_post}


def evaluate_price_action_plan(candles_by_tf, previous_state=None, *,
                               swing_left=2, swing_right=2,
                               impulse_atr_mult=2.0, impulse_max_bars=5,
                               rejection_lookback=8, rejection_wick_ratio=0.5,
                               zone_stop_atr_mult=0.5, min_rr=1.5,
                               entry_mode="pullback", bos_left=1, bos_right=1):
    """
    Run the 4-step AND-chain over a bundle of completed-candle series
        {"4H": [...], "1H": [...], "15m": [...], "5m": [...]}
    (each oldest -> newest) and return a plan dict, or a no-trade dict naming
    the step that failed. This is the single price-action "candles -> plan"
    path, shared by the live report, fill_checker and the backtest.

    previous_state (optional) carries the last run's {"zone_id", "direction"};
    it's used only to avoid re-emitting a signal for a zone we already flagged.
    """
    previous_state = previous_state or {}
    c4h = candles_by_tf.get("4H", [])
    c1h = candles_by_tf.get("1H", [])
    c15 = candles_by_tf.get("15m", [])
    c5 = candles_by_tf.get("5m", [])
    if not (c4h and c1h and c15 and c5):
        return _no_trade("Insufficient multi-timeframe data — sitting out.")

    # Step 1 — 4H trend.
    trend = swing_trend(c4h, swing_left, swing_right)
    if trend is None:
        return _no_trade("4H structure is ranging (no clean HH/HL or LH/LL) — sitting out.")
    direction = "long" if trend == "bullish" else "short"

    # Step 2 — 1H reversal zone.
    atr_1h = bot.atr(c1h)
    zone = find_reversal_zone(c1h, direction, atr_1h, swing_left, swing_right,
                              impulse_atr_mult, impulse_max_bars)
    if zone is None:
        return _no_trade(f"No fresh 1H reversal zone with an aggressive departure "
                         f"({impulse_atr_mult}x ATR) for the {direction} side — sitting out.",
                         raw_direction=direction)
    zone_low, zone_high, zone_id = zone

    # Skip a zone we've already signalled (avoids re-firing every run).
    if previous_state.get("zone_id") == zone_id and previous_state.get("direction") == direction:
        return _no_trade("Already signalled this 1H zone — waiting for a fresh setup.",
                         raw_direction=direction, zone_id=zone_id, suppress_post=True)

    # Step 3 — 15M reaction.
    if not detect_rejection(c15, (zone_low, zone_high), direction,
                            rejection_lookback, rejection_wick_ratio):
        return _no_trade("Price hasn't reacted to the 1H zone on 15M yet (no rejection) — no trade.",
                         raw_direction=direction, zone_id=zone_id)

    # Step 4 — 5M break of structure.
    bos_level = detect_bos(c5, direction, bos_left, bos_right)
    if bos_level is None:
        opp = "lower-high" if direction == "long" else "higher-low"
        return _no_trade(f"15M rejection is in, but 5M hasn't broken the last {opp} yet — "
                         f"waiting for confirmation.", raw_direction=direction, zone_id=zone_id)

    # All four align — build entry / stop / target.
    price = c5[-1]["close"]
    atr_5m = bot.atr(c5)
    if entry_mode == "market":
        entry = price
    else:  # pullback: retest the broken structure level
        entry = bos_level

    if direction == "long":
        stop = zone_low - zone_stop_atr_mult * atr_5m
        target = _nearest_target(c1h, direction, entry, swing_left, swing_right)
        if target is None:
            target = entry + (entry - stop) * min_rr
        risk = entry - stop
        reward = target - entry
    else:
        stop = zone_high + zone_stop_atr_mult * atr_5m
        target = _nearest_target(c1h, direction, entry, swing_left, swing_right)
        if target is None:
            target = entry - (stop - entry) * min_rr
        risk = stop - entry
        reward = entry - target

    if risk <= 0 or reward <= 0:
        return _no_trade("Couldn't compute a sane risk:reward from the zone/structure — sitting out.",
                         raw_direction=direction, zone_id=zone_id)
    rr = reward / risk
    if rr < min_rr:
        return _no_trade(f"Risk:reward is {rr:.2f}, below the {min_rr} threshold — sitting out.",
                         raw_direction=direction, zone_id=zone_id)

    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rr": rr,
        "raw_direction": direction,
        "zone_id": zone_id,
        "zone": (zone_low, zone_high),
        "bos_level": bos_level,
        "trend": trend,
    }


def revalidate_fill(candles_by_tf, direction, *, swing_left=2, swing_right=2):
    """
    Fill-time re-check for a pending price-action entry.

    The break-of-structure and the 15M rejection both happen *before* the
    retest that actually fills the order, so re-running the full 4-step chain
    here would wrongly reject every genuine retest — by the time price pulls
    back to the entry there is no *current* 5M BOS to detect. The thesis that
    must still hold at fill time is the top-level filter: the 4H trend. If the
    higher-timeframe structure has flipped or gone to range while we waited for
    the retest, the setup is stale and we skip it — the same "setup decayed by
    fill time" guard the indicator strategy applies, just keyed on structure
    instead of the bias score.

    Returns a plan-shaped dict ({"direction": ...}) so it drops straight into
    the shared re-check callers (fill_checker and backtest.simulate_trade),
    which only look at ["direction"].
    """
    trend = swing_trend(candles_by_tf.get("4H", []), swing_left, swing_right)
    still_valid = (
        (trend == "bullish" and direction == "long")
        or (trend == "bearish" and direction == "short")
    )
    if still_valid:
        return {"direction": direction}
    return {"direction": None,
            "reason": f"4H trend no longer agrees with the pending {direction} — setup went stale."}
