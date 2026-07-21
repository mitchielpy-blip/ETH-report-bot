"""
ETH Hourly Trading Report Bot
------------------------------
Fetches ETH-USDT-SWAP candles from OKX's public API, computes basic
technical indicators (RSI, MACD, EMA, support/resistance), and posts
a formatted report to a Discord channel via webhook.

This is a template for personal research/education. It is NOT financial
advice, and the "confidence" / "win rate" figures are simple heuristics,
not a validated predictive model. Always apply your own judgment and
risk management before trading.

Setup:
  1. pip install -r requirements.txt
  2. Set environment variables (see README.md):
       DISCORD_WEBHOOK_URL
  3. Run manually:  python eth_report_bot.py
  4. For hourly auto-posting, see the GitHub Actions workflow included.

CHANGELOG (audit fixes):
  * fetch_candles now discards the in-progress (unconfirmed) candle that
    OKX returns as the newest row. Previously every indicator — RSI,
    MACD, EMA, ATR, and especially volume_ratio — was computed on a
    candle only minutes old, which made live behavior diverge from the
    backtest (which only ever sees completed candles). volume_ratio was
    the worst hit: a 7-minute-old candle almost always looks "low
    volume", permanently dampening the bias score toward neutral.
  * Pending unfilled entries are now preserved in state.json for up to
    PENDING_ENTRY_LIFETIME_HOURS instead of being wiped by the next
    hourly run. This matches the backtest, which gives a pullback entry
    ENTRY_WAIT_CANDLES bars to fill, and keeps fill_checker.py watching
    the level for the full window.
"""

import os
import sys
import time
import json
import csv
import requests
from datetime import datetime, timezone, timedelta

SGT = timezone(timedelta(hours=8))  # Singapore Time, UTC+8, no DST

OKX_BASE = os.environ.get("OKX_BASE", "https://www.okx.com")
# Optional proxy for OKX market-data calls ONLY (Discord posts and the git
# push in CI stay direct). OKX geo-blocks some datacenter IPs — notably
# GitHub-hosted Actions runners, which live on US Azure ranges — and answers
# those requests with an HTTP 3xx redirect instead of data (the "307" symptom).
# Point OKX_PROXY at a proxy in a region OKX serves to route just these
# requests through it. Unset = call OKX directly (fine when your own IP is
# allowed, e.g. local runs).
OKX_PROXY = os.environ.get("OKX_PROXY")
OKX_PROXIES = {"https": OKX_PROXY, "http": OKX_PROXY} if OKX_PROXY else None
INST_ID = os.environ.get("INST_ID", "ETH-USDT-SWAP")   # perpetual swap
ASSET = INST_ID.split("-")[0]                            # e.g. "ETH", "BTC" — used in report titles
BAR = os.environ.get("BAR", "1H")                       # candle size
HTF_BAR = os.environ.get("HTF_BAR", "4H")                # higher-timeframe filter
LOOKBACK = 200                                            # completed candles to analyze
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SIGNALS_LOG_FILE = os.environ.get("SIGNALS_LOG_FILE", "signals_log.csv")
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

# Trade-plan heuristics (all tunable). None of this is validated against
# real performance — treat as a starting template for your own rules.
LONG_SCORE_MIN = float(os.environ.get("LONG_SCORE_MIN", 62))   # score >= this -> consider long
SHORT_SCORE_MAX = float(os.environ.get("SHORT_SCORE_MAX", 45))  # score <= this -> consider short
# Contrarian override — FADE the bias score. When True, every trade the strategy
# would take is entered on the OPPOSITE side: a long-zone score (>= LONG_SCORE_MIN)
# is traded short, a short-zone score (<= SHORT_SCORE_MAX) is traded long. The flip
# happens AFTER every native-direction gate (ADX / HTF / persistence / session) has
# run, so the SET of bars selected is identical to the fixed strategy — only the
# side changes. That isolates the single question "what if I'd taken the other side
# of every signal", motivated by score_calibration.py showing the raw score is
# anti-predictive on ETH/BTC (high score -> below-average forward returns).
#
# IMPORTANT — backtest-research knob only, default OFF so live is byte-identical.
# Do NOT set INVERT_SIGNAL on any live workflow (eth-report.yml / fill-checker.yml):
# it exists purely to A/B the fade hypothesis in the backtest before any of it is
# considered for live use.
INVERT_SIGNAL = os.environ.get("INVERT_SIGNAL", "0").strip().lower() in ("1", "true", "yes")
# Disable the higher-timeframe (HTF_BAR, default 4H) trend veto that stops longs
# in a bearish 4H trend and shorts in a bullish one. Exists to A/B whether that
# filter earns its keep: does dropping HTF-disagreeing trades add expectancy, or
# is it just trimming trades that would have been fine? The diagnostics-only
# By-HTF-trend bucketing can't answer that (it only sees trades the filter already
# let through) — you need a filter-on vs filter-off backtest, which this knob gives.
#
# IMPORTANT — backtest-research knob only, default OFF so live is byte-identical.
# Do NOT set DISABLE_HTF_FILTER on any live workflow (eth-report.yml /
# fill-checker.yml): it exists purely to measure the filter's value in the
# backtest before any change is considered for live use.
DISABLE_HTF_FILTER = os.environ.get("DISABLE_HTF_FILTER", "0").strip().lower() in ("1", "true", "yes")
ATR_SL_MULT = float(os.environ.get("ATR_SL_MULT", 1.5))         # stop distance = ATR * this
MIN_RR = float(os.environ.get("MIN_RR", 1.5))                    # minimum reward:risk to publish a plan
PULLBACK_ATR_MULT = float(os.environ.get("PULLBACK_ATR_MULT", 0.7))  # how deep a pullback entry to seek, in ATRs
# How the entry level is placed relative to the current price:
#   "pullback" (default, live) — wait for a shallow retrace against the signal,
#   "market"                  — take it now at the current price,
#   "breakout"                — enter only as price extends further in the
#                               signal's direction (continuation).
# Only "pullback" is wired into the live bot + fill_checker; "market"/"breakout"
# exist so entry_method_backtest.py can compare them head-to-head before any is
# considered for live use.
ENTRY_MODE = os.environ.get("ENTRY_MODE", "pullback")
# Updated from 1.0 -> 0.7 on the basis of backtest_sweep.py results (12mo, 1H):
# 0.7 raised fill rate 11.1%->20.3%, win rate 45.5%->52.2%, and net expectancy
# 0.145R->0.314R per trade vs the old 1.0 setting, with both half-window splits
# staying solidly positive (+0.335R / +0.293R). See pullback_sweep.csv for the
# full comparison across 1.0/0.7/0.5/0.3 if this ever needs revisiting.
ADX_MIN = float(os.environ.get("ADX_MIN", 20))                        # skip trades when trend strength is below this
VOLUME_CONFIRM_RATIO = float(os.environ.get("VOLUME_CONFIRM_RATIO", 1.2))  # above-average volume amplifies conviction
VOLUME_LOW_RATIO = float(os.environ.get("VOLUME_LOW_RATIO", 0.7))          # below-average volume dampens conviction
# Trading-session filter. Comma-separated session keys to SIT OUT, keyed by the
# signal bar's UTC hour (asia = 00-08 UTC / 08-16 SGT, europe = 08-16 UTC,
# us = 16-24 UTC — the same boundaries diagnostics.py buckets by). Empty
# (the default) trades every session, so the validated model is byte-unchanged
# unless you opt in. Set e.g. SKIP_SESSIONS=asia for an instrument whose
# diagnostics show a session is a persistent, out-of-sample drag (BTC's Asia
# session was, across two independent 12-month windows).
SKIP_SESSIONS = os.environ.get("SKIP_SESSIONS", "")
SKIP_SESSIONS_SET = frozenset(s.strip().lower() for s in SKIP_SESSIONS.split(",") if s.strip())
# Exit management. "fixed" (the default) is set-and-forget: a filled trade runs
# to its original stop, its target, or the hold timeout — exactly what the live
# bot delivers today (the hourly report posts entry/stop/target, fill_checker
# confirms the fill, and nothing manages the position after that). "breakeven"
# moves the stop up to entry once the trade has gone BREAKEVEN_AT_R in favour,
# turning a pullback into a scratch instead of a loss. "trailing" trails the stop
# behind the best price (see the TRAIL_* knobs below) so winners can run.
#
# IMPORTANT — backtest-research knob only. There is no live position-manager yet,
# so the live report/fill-checker have no way to tell you to move your stop. This
# MUST stay "fixed" on every live workflow, or the backtest would model an exit
# the live alerts don't deliver (a phantom edge) and stop describing the bot.
# Only backtest.py reads it, to measure whether breakeven beats fixed before any
# live wiring is built.
EXIT_MODEL = os.environ.get("EXIT_MODEL", "fixed")                       # fixed | breakeven | trailing
BREAKEVEN_AT_R = float(os.environ.get("BREAKEVEN_AT_R", 1.0))            # favourable R before the stop moves to entry
BREAKEVEN_BUFFER_R = float(os.environ.get("BREAKEVEN_BUFFER_R", 0.0))    # 0 = stop to exact entry; >0 leaves a small cushion past entry
# Trailing-stop knobs (EXIT_MODEL=trailing). Once the trade's favourable
# excursion clears TRAIL_AT_R, the stop trails TRAIL_DISTANCE_R behind the best
# price and never loosens. By default a trailing trade ignores the fixed target
# so winners can run past it (TRAIL_HONOR_TARGET=false); set it true to keep the
# target as a hard cap and use the trail only for downside protection.
TRAIL_AT_R = float(os.environ.get("TRAIL_AT_R", 1.0))                    # favourable R before the trail activates
TRAIL_DISTANCE_R = float(os.environ.get("TRAIL_DISTANCE_R", 1.0))       # how far (in R) the stop trails behind the best price
TRAIL_HONOR_TARGET = os.environ.get("TRAIL_HONOR_TARGET", "false").strip().lower() in ("1", "true", "yes")

# How long a pending (unfilled) pullback entry stays live before being
# discarded. Keep this equal to the backtest's ENTRY_WAIT_CANDLES (in
# hours, for 1H bars) and fill_checker's PENDING_ORDER_EXPIRY_HOURS so
# all three components agree on an order's lifetime.
# 24 (was 8): a longer fill window recovers "slow pullback" entries that
# arrive after ~8h instead of expiring. Validated in and out of sample: it
# lifts ETH expectancy +0.22R->+0.27R and BTC +0.33R->+0.39R (recent) with
# lower/flat drawdown, and is a wash on SOL (flat expectancy, a few more
# fills). Safe because the fill-time re-validation still discards a pending
# order whose direction has since flipped, so a staler signal can't fill wrong.
PENDING_ENTRY_LIFETIME_HOURS = float(os.environ.get("PENDING_ENTRY_LIFETIME_HOURS", 24))

# Which strategy the "candles -> plan" seam runs:
#   "indicator"    (default) — the original RSI/MACD/EMA/ADX/volume bias-score
#                              model on 1H + a 4H EMA trend filter.
#   "price_action"           — a 4-timeframe (4H/1H/15M/5M) structure/zone/
#                              rejection/break-of-structure model, implemented in
#                              price_action.py. See PRICE-ACTION tunables below.
# Both return the identical plan-dict shape, so state, logging, posting, the
# fill checker and the backtest all work either way.
STRATEGY = os.environ.get("STRATEGY", "indicator")

# --- PRICE-ACTION tunables (only used when STRATEGY=price_action) -----------
# The Threads strategy is discretionary; these turn its fuzzy language into
# mechanical thresholds you can sweep, exactly like PULLBACK_ATR_MULT. None of
# these are validated yet — backtest before relying on them.
PA_TIMEFRAMES = {
    "4H": os.environ.get("PA_TREND_BAR", "4H"),    # Step 1 — trend structure
    "1H": os.environ.get("PA_ZONE_BAR", "1H"),     # Step 2 — reversal zone
    "15m": os.environ.get("PA_REACT_BAR", "15m"),  # Step 3 — rejection
    "5m": os.environ.get("PA_TRIGGER_BAR", "5m"),  # Step 4 — break of structure
}
PA_SWING_LEFT = int(os.environ.get("PA_SWING_LEFT", 2))          # pivot bars to the left
PA_SWING_RIGHT = int(os.environ.get("PA_SWING_RIGHT", 2))         # pivot bars to the right (confirmation lag)
PA_IMPULSE_ATR_MULT = float(os.environ.get("PA_IMPULSE_ATR_MULT", 2.0))  # "aggressive" departure, in 1H ATRs
PA_IMPULSE_MAX_BARS = int(os.environ.get("PA_IMPULSE_MAX_BARS", 5))       # ...within this many 1H bars
PA_REJECTION_LOOKBACK = int(os.environ.get("PA_REJECTION_LOOKBACK", 8))   # 15M bars to look back for a rejection
PA_REJECTION_WICK_RATIO = float(os.environ.get("PA_REJECTION_WICK_RATIO", 0.5))  # min wick share of a rejection candle
PA_ZONE_STOP_ATR_MULT = float(os.environ.get("PA_ZONE_STOP_ATR_MULT", 0.5))      # stop buffer beyond the zone, in 5M ATRs
PA_BOS_LEFT = int(os.environ.get("PA_BOS_LEFT", 1))              # 5M swing pivot (left) for the BOS check
PA_BOS_RIGHT = int(os.environ.get("PA_BOS_RIGHT", 1))            # 5M swing pivot (right) for the BOS check


def parse_candle_row(row):
    """
    Parse one OKX candle row into our candle dict. OKX returns
    [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm];
    we keep the OHLCV fields. Shared by the live fetch and the backtest's
    history fetch so both read the API's rows identically.
    """
    return {
        "ts": int(row[0]),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "vol": float(row[5]),
    }


def okx_get(path, params=None, timeout=10):
    """
    GET a public OKX endpoint through the optional OKX_PROXY and return the
    parsed JSON body.

    Redirects are NOT followed: OKX's v5 market endpoints always answer 200
    with JSON, so a 3xx here means the request came from an IP OKX geo-blocks
    (classically a GitHub-hosted runner). We surface that as a clear error
    naming OKX_PROXY, instead of silently following the redirect to an HTML
    page and failing later with a confusing JSON-decode error.
    """
    r = requests.get(f"{OKX_BASE}{path}", params=params, timeout=timeout,
                     proxies=OKX_PROXIES, allow_redirects=False)
    if 300 <= r.status_code < 400:
        raise RuntimeError(
            f"OKX redirected the request ({r.status_code} -> {r.headers.get('Location')}). "
            "This almost always means the call came from an IP OKX geo-blocks "
            "(e.g. a GitHub-hosted Actions runner). Set the OKX_PROXY secret to a "
            "proxy in a region OKX serves."
        )
    r.raise_for_status()
    return r.json()


def fetch_candles(inst_id=INST_ID, bar=BAR, limit=LOOKBACK):
    """
    Fetch completed candles from OKX, oldest -> newest.

    OKX returns newest-first: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
    where confirm == "1" means the candle has closed. The newest row is
    the current in-progress candle — we request one extra and drop any
    unconfirmed rows so every indicator only ever sees completed bars,
    exactly like the backtest does.
    """
    # +2 head-room: the current bar is always unconfirmed, and right at
    # the turn of the hour there can briefly be two.
    params = {"instId": inst_id, "bar": bar, "limit": str(min(limit + 2, 300))}

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = okx_get("/api/v5/market/candles", params)
            if data.get("code") != "0":
                raise RuntimeError(f"OKX error: {data}")
            break
        except (requests.RequestException, RuntimeError, ValueError) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * attempt
                print(f"fetch_candles attempt {attempt} failed ({e}); retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
    else:
        raise RuntimeError(f"fetch_candles failed after {MAX_RETRIES} attempts: {last_err}")

    # Keep only confirmed (closed) candles — this is the audit fix.
    rows = [row for row in data["data"] if len(row) > 8 and row[8] == "1"]
    rows.reverse()  # oldest -> newest
    rows = rows[-limit:]
    return [parse_candle_row(row) for row in rows]


def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema_last(closes, period):
    """Last EMA value, gracefully shrinking the period if data is short.
    Shared by the live bot and the backtest so both use identical logic."""
    return ema(closes, min(period, len(closes)))[-1]


def rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(closes, fast=12, slow=26, signal=9):
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    hist = macd_line[-1] - signal_line[-1]
    return macd_line[-1], signal_line[-1], hist


def atr(candles, period=14):
    """Average True Range — used to size stop-loss distance to current volatility."""
    trs = []
    for i in range(1, len(candles)):
        h, l, prev_c = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period


def adx(candles, period=14):
    """
    Average Directional Index (Wilder's method) — measures trend strength,
    not direction. Low ADX = flat/choppy market where pullback strategies
    tend to underperform; high ADX = a real trend is in place.
    Returns None if there isn't enough data yet.
    """
    if len(candles) < period * 2:
        return None

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        up_move = candles[i]["high"] - candles[i - 1]["high"]
        down_move = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i - 1]["close"]),
            abs(candles[i]["low"] - candles[i - 1]["close"]),
        )
        trs.append(tr)

    def wilder_smooth(values, period):
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + v)
        return smoothed

    smoothed_tr = wilder_smooth(trs, period)
    smoothed_plus_dm = wilder_smooth(plus_dm, period)
    smoothed_minus_dm = wilder_smooth(minus_dm, period)

    dx_values = []
    for tr_s, pdm_s, mdm_s in zip(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm):
        if tr_s == 0:
            continue
        plus_di = 100 * pdm_s / tr_s
        minus_di = 100 * mdm_s / tr_s
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum != 0 else 0
        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    adx_smoothed = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx_smoothed = (adx_smoothed * (period - 1) + dx) / period

    return adx_smoothed


def volume_ratio(candles, lookback=20):
    """
    Latest completed candle's volume relative to the average of the
    preceding `lookback` candles. >1 means above-average participation
    (a breakout or continuation is more likely to be "real"); <1 means
    below-average (more likely to be noise or a low-conviction move that
    fails). Returns None if there isn't enough data yet.

    Note: fetch_candles now guarantees candles[-1] is a *completed* bar,
    so this comparison is finally apples-to-apples with the backtest.
    """
    if len(candles) < lookback + 1:
        return None
    recent = candles[-(lookback + 1):-1]  # exclude the latest candle itself
    avg_vol = sum(c["vol"] for c in recent) / len(recent)
    if avg_vol == 0:
        return None
    return candles[-1]["vol"] / avg_vol


def _cluster_levels(values, price, n_levels, tolerance_pct=0.003):
    """Merge nearby price levels (within tolerance_pct of price) into one."""
    values = sorted(values)
    clusters = []
    for v in values:
        if clusters and abs(v - clusters[-1][-1]) <= price * tolerance_pct:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    merged = [sum(c) / len(c) for c in clusters]
    return merged[:n_levels] if len(merged) <= n_levels else merged


def support_resistance(candles, lookback=40, n_levels=3):
    """Cluster recent swing highs/lows into a handful of clean levels."""
    window = candles[-lookback:]
    price = window[-1]["close"]
    highs = [c["high"] for c in window]
    lows = [c["low"] for c in window]

    resistance_candidates = sorted(highs, reverse=True)[:15]
    support_candidates = sorted(lows)[:15]

    resistances = _cluster_levels(resistance_candidates, price, n_levels)
    supports = _cluster_levels(support_candidates, price, n_levels)

    resistances = sorted(resistances, reverse=True)[:n_levels]
    supports = sorted(supports)[-n_levels:]
    return supports, resistances


def compute_bias_score(candles):
    """
    Heuristic 0-100 bias score (clamped to 5-95) from completed candles.

    Pure and network-free, keyed only on the candle series — the single
    source of truth for the score, shared by the live report
    (build_report) and the backtest (evaluate_signal_at) so both score
    every candle identically. This is the same reason ema_last is shared:
    the moment the two paths score differently, the backtest silently
    stops describing the live bot. NOT a validated win-rate model.
    """
    closes = [c["close"] for c in candles]
    r = rsi(closes)
    _, _, hist = macd(closes)
    ema20 = ema_last(closes, 20)
    ema50 = ema_last(closes, 50)

    score = 50
    if r is not None:
        score += (r - 50) * 0.4
    score += 15 if ema20 > ema50 else -15
    score += 10 if hist > 0 else -10

    # Volume confirmation: above-average participation amplifies whatever
    # direction the other indicators already lean toward; below-average
    # volume dampens it back toward neutral (low participation = noise).
    vol_ratio = volume_ratio(candles)
    if vol_ratio is not None:
        deviation = score - 50
        if vol_ratio >= VOLUME_CONFIRM_RATIO:
            deviation *= 1.15
        elif vol_ratio <= VOLUME_LOW_RATIO:
            deviation *= 0.7
        score = 50 + deviation

    return max(5, min(95, round(score)))


def htf_trend_from_closes(closes):
    """
    Classify a higher-timeframe close series as 'bullish' / 'bearish' via
    EMA20 vs EMA50, or None if there are fewer than 20 closes. Shared by
    the live HTF fetch (higher_timeframe_trend) and the backtest's
    resampled HTF so both classify the trend identically.
    """
    if len(closes) < 20:
        return None
    return "bullish" if ema_last(closes, 20) > ema_last(closes, 50) else "bearish"


def higher_timeframe_trend(bar=HTF_BAR):
    """Fetch a higher timeframe and return 'bullish' / 'bearish' via EMA20 vs EMA50.
    fetch_candles already strips the in-progress candle, so this now uses
    only completed HTF bars — matching the backtest's resampled HTF."""
    try:
        htf_candles = fetch_candles(bar=bar, limit=100)
    except Exception as e:
        print(f"Could not fetch higher-timeframe data ({e}); skipping HTF filter.", file=sys.stderr)
        return None
    return htf_trend_from_closes([c["close"] for c in htf_candles])


def fetch_timeframes(timeframes=None):
    """
    Fetch the four completed-candle series the price-action strategy needs,
    keyed by role: {"4H": [...], "1H": [...], "15m": [...], "5m": [...]}, each
    oldest -> newest. Reuses fetch_candles (which already strips the in-progress
    candle), so every bar handed to price_action is closed — the same guarantee
    the backtest gives by resampling only completed bars. The live counterpart
    of the backtest's single-5M-feed-resampled-up approach.
    """
    timeframes = timeframes or PA_TIMEFRAMES
    # Higher timeframes need fewer bars; 5M needs a wide window to hold enough
    # structure for the BOS check without a huge fetch.
    limits = {"4H": 120, "1H": 120, "15m": 120, "5m": 200}
    bundle = {}
    for role, bar in timeframes.items():
        bundle[role] = fetch_candles(bar=bar, limit=limits.get(role, 120))
    return bundle


def _price_action_plan(candles_by_tf, previous_state=None):
    """Adapter: call the shared price_action evaluator with this module's
    env-configured tunables. Imported lazily so the default indicator path
    never depends on price_action."""
    import price_action
    return price_action.evaluate_price_action_plan(
        candles_by_tf, previous_state,
        swing_left=PA_SWING_LEFT, swing_right=PA_SWING_RIGHT,
        impulse_atr_mult=PA_IMPULSE_ATR_MULT, impulse_max_bars=PA_IMPULSE_MAX_BARS,
        rejection_lookback=PA_REJECTION_LOOKBACK, rejection_wick_ratio=PA_REJECTION_WICK_RATIO,
        zone_stop_atr_mult=PA_ZONE_STOP_ATR_MULT, min_rr=MIN_RR,
        entry_mode=ENTRY_MODE, bos_left=PA_BOS_LEFT, bos_right=PA_BOS_RIGHT,
    )


def _price_action_revalidate(candles_by_tf, direction):
    """Adapter for the price-action fill-time re-check (the retest-aware guard
    in price_action.revalidate_fill), shared by fill_checker and the backtest
    so both invalidate a stale pending entry identically."""
    import price_action
    return price_action.revalidate_fill(candles_by_tf, direction,
                                        swing_left=PA_SWING_LEFT, swing_right=PA_SWING_RIGHT)


def build_entry_levels(direction, price, atr_value, supports, resistances):
    """
    Place the entry, stop and target for a proposed trade. ENTRY_MODE selects
    how the entry sits relative to the current price:

      * "pullback" (default) — wait for a shallow, ATR-scaled retrace against
        the signal (long below price, short above), floored/capped at the
        nearest structural level. Better fill, but the move can leave without
        you.
      * "market" — take the signal now, at the current price.
      * "breakout" — enter only as price extends further in the signal's
        direction (long above price, short below): continuation, not retrace.

    The stop sits ATR_SL_MULT ATRs beyond the entry. Target is the nearest
    structural level in front of the entry, or an ATR-projected level giving
    MIN_RR when there's no structure to aim at. The pullback arm is unchanged
    from the original inline logic (the 19/75/5 and backtest characterizations
    pin that), so live behaviour is byte-identical while ENTRY_MODE=pullback.
    """
    if direction == "long":
        nearest_support = max([s for s in supports if s < price], default=None)
        nearest_resistance = min([r for r in resistances if r > price], default=None)
        if ENTRY_MODE == "market":
            entry = price
            target = nearest_resistance if nearest_resistance else entry + atr_value * ATR_SL_MULT * MIN_RR
        elif ENTRY_MODE == "breakout":
            entry = price + atr_value * PULLBACK_ATR_MULT
            res_above = min([r for r in resistances if r > entry], default=None)
            target = res_above if res_above else entry + atr_value * ATR_SL_MULT * MIN_RR
        else:  # pullback
            atr_pullback_entry = price - atr_value * PULLBACK_ATR_MULT
            entry = max(atr_pullback_entry, nearest_support) if nearest_support else atr_pullback_entry
            target = nearest_resistance if nearest_resistance else entry + atr_value * ATR_SL_MULT * MIN_RR
        stop = entry - atr_value * ATR_SL_MULT
    else:  # short
        nearest_resistance = min([r for r in resistances if r > price], default=None)
        nearest_support = max([s for s in supports if s < price], default=None)
        if ENTRY_MODE == "market":
            entry = price
            target = nearest_support if nearest_support else entry - atr_value * ATR_SL_MULT * MIN_RR
        elif ENTRY_MODE == "breakout":
            entry = price - atr_value * PULLBACK_ATR_MULT
            sup_below = max([s for s in supports if s < entry], default=None)
            target = sup_below if sup_below else entry - atr_value * ATR_SL_MULT * MIN_RR
        else:  # pullback
            atr_pullback_entry = price + atr_value * PULLBACK_ATR_MULT
            entry = min(atr_pullback_entry, nearest_resistance) if nearest_resistance else atr_pullback_entry
            target = nearest_support if nearest_support else entry - atr_value * ATR_SL_MULT * MIN_RR
        stop = entry + atr_value * ATR_SL_MULT
    return entry, stop, target


def session_for_hour(utc_hour):
    """Map a signal bar's UTC hour to a session key (asia/europe/us). The
    boundaries match diagnostics.py's bucket_session, so the SKIP_SESSIONS
    filter sits out exactly the buckets that analysis breaks results down by."""
    if 0 <= utc_hour < 8:
        return "asia"
    if 8 <= utc_hour < 16:
        return "europe"
    return "us"


def suggest_trade_plan(price, score, atr_value, supports, resistances, htf_trend=None, adx_value=None, previous_raw_direction=None, require_rr=True, session=None):
    """
    Rule-based entry/SL/TP suggestion. Returns a dict, or None if no setup
    clears the minimum reward:risk bar (mirrors "RR不合格，不開倉" logic).

    Direction is only proposed when the bias score is clearly one-sided,
    it agrees with the same raw direction from the previous hour (a
    persistence filter — a score that flickers to "long" for one hour
    and disappears is more likely noise than a real setup), the ADX and
    higher-timeframe checks pass, and the resulting reward:risk clears
    MIN_RR. Every return includes "raw_direction" so the caller can track
    it for next hour's persistence check, even when no trade results.

    require_rr gates on the reward:reward at *signal-generation* time (the
    default). The fill-time re-check passes require_rr=False: by then a
    pending order already exists with its entry/stop/target — and thus its
    R:R — locked in from when it was generated. Re-deriving fresh levels from
    the latest ATR and re-gating on *their* R:R would throw away a valid fill
    because a different, hypothetical trade wouldn't qualify. At fill time we
    only care whether the directional thesis still holds, not whether a
    freshly-minted plan would clear MIN_RR.
    """
    if score >= LONG_SCORE_MIN:
        raw_direction = "long"
    elif score <= SHORT_SCORE_MAX:
        raw_direction = "short"
    else:
        raw_direction = None

    if raw_direction is None:
        return {"direction": None, "reason": "Signal isn't clear enough (score is in the neutral zone) — sitting out this hour.", "raw_direction": None}

    if adx_value is not None and adx_value < ADX_MIN:
        return {"direction": None, "reason": f"ADX {adx_value:.1f} is below {ADX_MIN} — market looks flat/choppy, sitting out.", "raw_direction": raw_direction}

    if session is not None and session in SKIP_SESSIONS_SET:
        # A filtered session is a deterministic, known-in-advance skip, so it's
        # not "news" worth pinging the channel about — suppress_post keeps the
        # hourly report silent (should_post honours it). The signal is still
        # logged, and any live pending order from an allowed session is still
        # kept alive / watched by fill_checker (which passes session=None).
        return {"direction": None,
                "reason": f"{session.capitalize()} session is filtered out for this instrument (SKIP_SESSIONS) — sitting out.",
                "raw_direction": raw_direction,
                "suppress_post": True}

    if previous_raw_direction != raw_direction:
        return {"direction": None, "reason": f"{raw_direction.capitalize()} signal just appeared this hour — waiting one more hour to confirm it's not noise.", "raw_direction": raw_direction}

    direction = raw_direction

    # HTF veto — gated by DISABLE_HTF_FILTER (backtest-research knob, default off;
    # live always runs the filter). When disabled, HTF-disagreeing trades are kept
    # so the backtest can measure the filter's contribution on/off.
    if not DISABLE_HTF_FILTER:
        if htf_trend == "bearish" and direction == "long":
            return {"direction": None, "reason": f"{HTF_BAR} trend is bearish — skipping long to avoid fighting the higher timeframe.", "raw_direction": raw_direction}
        if htf_trend == "bullish" and direction == "short":
            return {"direction": None, "reason": f"{HTF_BAR} trend is bullish — skipping short to avoid fighting the higher timeframe.", "raw_direction": raw_direction}

    # Fade override (backtest-research, default off). Flip to the opposite side
    # only now — after every native-direction gate above has decided this bar is
    # tradeable — so the selected bar SET matches the fixed strategy exactly and
    # only the side is inverted. raw_direction stays the score's native call so
    # the hour-to-hour persistence check is unaffected; the levels, R:R gate and
    # returned direction below all use the flipped side, i.e. the trade actually
    # taken. Never set on a live workflow (see INVERT_SIGNAL in the config block).
    if INVERT_SIGNAL:
        direction = "short" if direction == "long" else "long"

    entry, stop, target = build_entry_levels(direction, price, atr_value, supports, resistances)
    if direction == "long":
        risk = entry - stop
        reward = target - entry
    else:  # short
        risk = stop - entry
        reward = entry - target

    if risk <= 0 or reward <= 0:
        return {"direction": None, "reason": "Couldn't compute a sane risk:reward — sitting out this hour.", "raw_direction": raw_direction}

    rr = reward / risk
    # Gate on the R:R as it's actually shown (rounded to 2dp), so a setup the
    # report displays as e.g. "1.50" isn't rejected because its raw value was
    # 1.497. Without this, a true R:R just under the threshold rounds up on
    # screen and looks like a rejected 1.50, which is confusing. Skipped when
    # require_rr is False (the fill-time re-check) — see the docstring.
    if require_rr and round(rr, 2) < MIN_RR:
        return {"direction": None, "reason": f"Risk:reward is {rr:.2f}, below the {MIN_RR} threshold — sitting out this hour.", "raw_direction": raw_direction}

    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rr": rr,
        "raw_direction": raw_direction,
    }


# Sentinel so evaluate_plan can tell "caller passed no HTF trend, fetch it
# live" apart from "caller explicitly passed htf_trend=None (trend unknown)".
# The backtest MUST always pass an explicit value so it never hits the network.
_HTF_UNSET = object()
# Same idea for the price-action bundle: unset -> fetch the four timeframes
# live; passed explicitly (by the backtest) -> never hit the network.
_PA_UNSET = object()


def evaluate_plan(candles, previous_raw_direction=None, htf_trend=_HTF_UNSET,
                  candles_by_tf=_PA_UNSET, previous_state=None, require_rr=True):
    """
    Derive the trade plan for a completed-candle series — the decision half of
    build_report, without any of the report formatting.

    This is the single "candles -> plan" path, shared by:
      * build_report            — the hourly report,
      * fill_checker            — the re-check when a pending pullback entry is
                                  finally touched (so a setup that has decayed
                                  by fill time is skipped live, exactly as the
                                  backtest discards it), and
      * backtest.evaluate_signal_at — via delegation.
    Deriving the plan one way for the live report and another way for the fill
    re-check or the backtest is precisely how a backtest silently stops
    describing the bot — so there is only this one path.

    STRATEGY selects the model. For "price_action" the plan comes from the
    four-timeframe bundle (fetched live when candles_by_tf is unset, or passed
    in by the backtest); `candles` is still accepted so the shared callers'
    signatures don't change. For "indicator" (default) the original logic runs
    unchanged: htf_trend may be passed in to reuse an already-fetched
    higher-timeframe read (build_report does this); left unset it is fetched
    live, and the backtest always passes its own resampled trend explicitly.
    """
    if STRATEGY == "price_action":
        if candles_by_tf is _PA_UNSET:
            candles_by_tf = fetch_timeframes()
        return _price_action_plan(candles_by_tf, previous_state)

    price = candles[-1]["close"]
    supports, resistances = support_resistance(candles)
    atr_value = atr(candles)
    adx_value = adx(candles)
    score = compute_bias_score(candles)
    if htf_trend is _HTF_UNSET:
        htf_trend = higher_timeframe_trend()
    # Session of the signal bar itself (its completed-candle UTC hour), so the
    # optional SKIP_SESSIONS gate is evaluated identically live and in the
    # backtest — both reach suggest_trade_plan through this one path. Like the
    # R:R gate, this is a signal-GENERATION filter, so it's only applied when
    # require_rr is True; at fill time (require_rr=False) we pass session=None so
    # a pending order generated in an allowed session isn't discarded just
    # because its entry happens to get touched during a filtered session.
    session = (session_for_hour(datetime.fromtimestamp(candles[-1]["ts"] / 1000, tz=timezone.utc).hour)
               if require_rr else None)
    return suggest_trade_plan(price, score, atr_value, supports, resistances,
                              htf_trend, adx_value, previous_raw_direction, require_rr=require_rr,
                              session=session)


def build_report(candles, previous_raw_direction=None):
    closes = [c["close"] for c in candles]
    price = closes[-1]
    r = rsi(closes)
    macd_line, signal_line, hist = macd(closes)
    ema20 = ema_last(closes, 20)
    ema50 = ema_last(closes, 50)
    supports, resistances = support_resistance(candles)

    trend = "bullish structure" if ema20 > ema50 else "bearish structure"
    momentum = "momentum firming up" if hist > 0 else "momentum fading"

    # Bias score comes from the shared scorer so the live report and the
    # backtest can never drift apart. vol_ratio is still read here for the
    # display line below.
    score = compute_bias_score(candles)
    vol_ratio = volume_ratio(candles)
    atr_value = atr(candles)  # the ruler that sizes the entry/stop/target distances below

    nearest_support = max([s for s in supports if s < price], default=supports[0] if supports else None)
    nearest_resistance = min([res for res in resistances if res > price], default=resistances[0] if resistances else None)
    adx_value = adx(candles)
    # The plan is derived by the shared evaluate_plan (same path the fill-time
    # re-check and the backtest use). We pass the HTF read we just fetched so
    # it isn't fetched twice.
    htf_trend = higher_timeframe_trend()
    plan = evaluate_plan(candles, previous_raw_direction, htf_trend=htf_trend)

    lines = []
    lines.append(f"**{ASSET} Hourly Report · {datetime.now(SGT).strftime('%Y-%m-%d %H:%M')} SGT**")
    lines.append(f"Price: ${price:,.2f} (last completed {BAR} close)")
    lines.append(f"Trend ({BAR}): {trend}, {momentum}")
    if htf_trend:
        lines.append(f"Higher-TF trend ({HTF_BAR}): {htf_trend}")
    if adx_value is not None:
        lines.append(f"ADX(14): {adx_value:.1f} ({'trending' if adx_value >= ADX_MIN else 'flat/choppy'})")
    if vol_ratio is not None:
        vol_label = "confirming" if vol_ratio >= VOLUME_CONFIRM_RATIO else ("weak" if vol_ratio <= VOLUME_LOW_RATIO else "normal")
        lines.append(f"Volume: {vol_ratio:.2f}x average ({vol_label})")
    if atr_value:
        lines.append(f"ATR(14): ${atr_value:,.2f} ({atr_value / price * 100:.1f}% of price) — sizes entry/stop/target; stop sits {ATR_SL_MULT:g}x ATR away")
    lines.append(f"RSI(14): {r:.1f}" if r else "RSI: insufficient data")
    lines.append(f"MACD histogram: {hist:+.2f}")
    lines.append(f"Bias score: {score}/100 (a rough heuristic, not a win rate)")
    if supports:
        lines.append(f"Key support: {', '.join(f'{s:,.0f}' for s in supports)}")
    if resistances:
        lines.append(f"Key resistance: {', '.join(f'{rr:,.0f}' for rr in resistances)}")
    if nearest_support and nearest_resistance:
        lines.append(f"Nearest range: {nearest_support:,.0f} - {nearest_resistance:,.0f}")

    lines.append("")
    if plan["direction"]:
        dir_label = "LONG" if plan["direction"] == "long" else "SHORT"
        lines.append(f"**Suggested direction: {dir_label}**")
        lines.append(f"Suggested entry: ${plan['entry']:,.2f}")
        lines.append(f"Stop-loss: ${plan['stop']:,.2f}")
        lines.append(f"Take-profit: ${plan['target']:,.2f}")
        lines.append(f"Risk:Reward: about 1:{plan['rr']:.2f}")
    else:
        lines.append(f"**Suggested direction: No entry**")
        lines.append(plan["reason"])

    lines.append("\n_Auto-generated from technical indicators only — not a win rate, not investment advice. Entry levels are rule-based estimates. Confirm risk and position size yourself before placing any order._")
    return "\n".join(lines), plan


def build_price_action_report(candles_by_tf, previous_state=None):
    """
    Report body for STRATEGY=price_action. Shows the 4-timeframe checklist
    (4H trend -> 1H zone -> 15M rejection -> 5M break of structure) and the
    resulting plan. The plan comes from the shared evaluate_plan so this report
    can never diverge from the fill-checker or the backtest.
    """
    import price_action as pa

    c4h, c1h, c5 = candles_by_tf["4H"], candles_by_tf["1H"], candles_by_tf["5m"]
    price = c5[-1]["close"]
    plan = evaluate_plan(c1h, candles_by_tf=candles_by_tf, previous_state=previous_state)

    trend = pa.swing_trend(c4h, PA_SWING_LEFT, PA_SWING_RIGHT)
    trend_label = {"bullish": "bullish (HH/HL) — longs only",
                   "bearish": "bearish (LH/LL) — shorts only"}.get(trend, "ranging — stand aside")
    atr_1h = atr(c1h)  # 1H volatility — sizes the impulse threshold and the zone-stop buffer

    lines = []
    lines.append(f"**{ASSET} Price-Action Report · {datetime.now(SGT).strftime('%Y-%m-%d %H:%M')} SGT**")
    lines.append(f"Price: ${price:,.2f} (last completed {PA_TIMEFRAMES['5m']} close)")
    if atr_1h:
        lines.append(f"ATR({PA_TIMEFRAMES['1H']}): ${atr_1h:,.2f} ({atr_1h / price * 100:.1f}% of price) — sizes the zone & stop buffer")
    lines.append(f"1. Trend ({PA_TIMEFRAMES['4H']}): {trend_label}")
    if plan.get("zone"):
        zl, zh = plan["zone"]
        lines.append(f"2. Zone ({PA_TIMEFRAMES['1H']}): ${zl:,.2f} - ${zh:,.2f}")
    if plan["direction"]:
        lines.append(f"3. Reaction ({PA_TIMEFRAMES['15m']}): rejection confirmed")
        lines.append(f"4. Confirmation ({PA_TIMEFRAMES['5m']}): broke structure at ${plan['bos_level']:,.2f}")

    lines.append("")
    if plan["direction"]:
        dir_label = "LONG" if plan["direction"] == "long" else "SHORT"
        lines.append(f"**Suggested direction: {dir_label}**")
        lines.append(f"Suggested entry: ${plan['entry']:,.2f}")
        lines.append(f"Stop-loss: ${plan['stop']:,.2f}")
        lines.append(f"Take-profit: ${plan['target']:,.2f}")
        lines.append(f"Risk:Reward: about 1:{plan['rr']:.2f}")
    else:
        lines.append("**Suggested direction: No entry**")
        lines.append(plan["reason"])

    lines.append("\n_Auto-generated from price-action rules only — not a win rate, not investment advice. Entry levels are rule-based estimates. Confirm risk and position size yourself before placing any order._")
    return "\n".join(lines), plan


def fetch_ticker_price(inst_id=None):
    """Lightweight single current-price check — much cheaper than a full candle fetch."""
    inst_id = inst_id or INST_ID
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = okx_get("/api/v5/market/ticker", {"instId": inst_id})
            if data.get("code") != "0" or not data.get("data"):
                raise RuntimeError(f"OKX ticker error: {data}")
            return float(data["data"][0]["last"])
        except (requests.RequestException, RuntimeError, ValueError, KeyError, IndexError) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"fetch_ticker_price failed after {MAX_RETRIES} attempts: {last_err}")


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def pending_order_is_live(state):
    """
    True if state holds an unfilled pending entry that hasn't expired.
    Lifetime mirrors the backtest's ENTRY_WAIT_CANDLES so live and
    simulated order handling agree.
    """
    if not state.get("direction") or state.get("entry") is None:
        return False
    if state.get("filled"):
        return False
    generated_at_ts = state.get("generated_at_ts")
    if not generated_at_ts:
        return False
    age_hours = (datetime.now(timezone.utc).timestamp() * 1000 - generated_at_ts) / (3600 * 1000)
    return age_hours <= PENDING_ENTRY_LIFETIME_HOURS


# Distinguishes "state file predates the last_posted_direction field" from
# "we last posted a genuine No-entry (None)". Only the former should fall back
# to the pending-order direction for the dedup below.
_LAST_POSTED_UNSET = object()


def should_post(plan, previous_state):
    """
    Decide whether this run's report is worth posting, or is just the same
    signal we already announced (which would spam the channel every hour).

    Indicator strategy: a one-sided bias score can persist for many hours, so
    suggest_trade_plan re-issues an essentially identical directional plan every
    run (only the ATR/price-derived entry drifts a few cents). We post only when
    the directional recommendation *changes* from the last thing we actually
    posted — a fresh signal (No-entry -> long/short), a flip (long <-> short), or
    a stand-down (long/short -> No-entry). A standing long/short no longer pings
    the channel hourly.

    price_action strategy: left on its original rule. Its own suppress_post /
    zone_id dedup already stops a live zone re-announcing itself, and two
    distinct zones that happen to share a direction are genuinely different
    trades that should each post — so same-direction posts are NOT suppressed
    there.
    """
    # A "no news" no-trade never posts, so it doesn't spam the channel: the
    # price-action strategy re-seeing a zone it already signalled, or the
    # indicator strategy sitting out a filtered SKIP_SESSIONS session (a
    # deterministic, known-in-advance skip). The signal is still logged either
    # way; we just don't ping.
    if plan.get("suppress_post"):
        return False

    current_direction = plan["direction"]

    if STRATEGY == "indicator":
        last_posted = previous_state.get("last_posted_direction", _LAST_POSTED_UNSET)
        if last_posted is _LAST_POSTED_UNSET:
            # State written before this field existed: approximate the last
            # posted direction with the pending-order direction so the first
            # run after deploy doesn't re-post a still-standing signal.
            last_posted = previous_state.get("direction")
        return current_direction != last_posted

    previous_direction = previous_state.get("direction")
    if current_direction is None and previous_direction is None:
        return False
    return True


def log_signal(price, plan):
    """
    Append every generated signal (whether it's a real trade or "no
    entry") to a CSV log. This is what lets you later check real forward
    performance against what the backtest predicted — the one test a
    backtest alone can never give you.
    """
    row = {
        "logged_at_ts": int(datetime.now(timezone.utc).timestamp() * 1000),
        "price": price,
        "direction": plan["direction"] or "",
        "entry": plan.get("entry", ""),
        "stop": plan.get("stop", ""),
        "target": plan.get("target", ""),
        "rr": plan.get("rr", ""),
        "reason": plan.get("reason", ""),
    }
    file_exists = os.path.isfile(SIGNALS_LOG_FILE)
    with open(SIGNALS_LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def post_to_discord(content):
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set — printing report instead:\n")
        print(content)
        return

    payload = {"content": content}
    # Optional display-name override: lets multiple bots share one webhook/
    # channel while appearing under different names (e.g. "BTC Pulse").
    # If unset, posts under the webhook's default configured name.
    bot_name = os.environ.get("DISCORD_BOT_NAME")
    if bot_name:
        payload["username"] = bot_name

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
            if resp.status_code < 300:
                return
            last_err = f"{resp.status_code}: {resp.text}"
        except requests.RequestException as e:
            last_err = str(e)
        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF_SECONDS * attempt
            print(f"post_to_discord attempt {attempt} failed ({last_err}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)

    print(f"Discord post failed after {MAX_RETRIES} attempts: {last_err}", file=sys.stderr)
    sys.exit(1)


def main():
    previous_state = load_state()

    if STRATEGY == "price_action":
        bundle = fetch_timeframes()
        if any(len(bundle[role]) < 30 for role in bundle):
            print("Not enough candle data returned across timeframes.", file=sys.stderr)
            sys.exit(1)
        report, plan = build_price_action_report(bundle, previous_state)
        price = bundle["5m"][-1]["close"]
    else:
        candles = fetch_candles()
        if len(candles) < 30:
            print("Not enough candle data returned.", file=sys.stderr)
            sys.exit(1)
        report, plan = build_report(candles, previous_state.get("last_raw_direction"))
        price = candles[-1]["close"]

    print("--- Generated report (always logged here, whether or not it posts) ---")
    print(report)
    print("--- end report ---")

    log_signal(price, plan)

    posted = should_post(plan, previous_state)
    if posted:
        post_to_discord(report)
    else:
        print("No change from the previously posted signal — skipping post to avoid noise.")

    # --- State handling (audit fix) ---
    # A new directional plan always replaces whatever was pending.
    # A "no entry" hour no longer wipes a still-live pending order —
    # the order keeps its ENTRY_WAIT window, exactly as the backtest
    # simulates it, and fill_checker.py keeps watching the level.
    if plan["direction"]:
        new_state = {
            "direction": plan["direction"],
            "last_raw_direction": plan.get("raw_direction"),
            "entry": plan["entry"],
            "stop": plan["stop"],
            "target": plan["target"],
            "rr": plan["rr"],
            "generated_at_ts": int(datetime.now(timezone.utc).timestamp() * 1000),
            "filled": False,
        }
        # price_action dedupe key: while this entry stays pending the same 1H
        # zone won't re-fire (see evaluate_price_action_plan).
        if plan.get("zone_id") is not None:
            new_state["zone_id"] = plan["zone_id"]
    elif pending_order_is_live(previous_state):
        new_state = dict(previous_state)
        new_state["last_raw_direction"] = plan.get("raw_direction")
        print(f"Keeping pending {previous_state['direction']} entry at {previous_state['entry']} alive "
              f"(within its {PENDING_ENTRY_LIFETIME_HOURS:.0f}h fill window).")
    else:
        new_state = {"direction": None, "last_raw_direction": plan.get("raw_direction")}

    # Remember what we actually posted this run so next run's should_post can
    # tell a genuinely new/changed signal from an unchanged standing one. If we
    # didn't post, carry the previous value forward unchanged (falling back to
    # the pending-order direction for pre-existing state files).
    if posted:
        new_state["last_posted_direction"] = plan["direction"]
    else:
        new_state["last_posted_direction"] = previous_state.get(
            "last_posted_direction", previous_state.get("direction"))

    save_state(new_state)


if __name__ == "__main__":
    main()
