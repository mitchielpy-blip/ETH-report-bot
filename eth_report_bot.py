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
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone

OKX_BASE = "https://www.okx.com"
INST_ID = os.environ.get("INST_ID", "ETH-USDT-SWAP")   # perpetual swap
BAR = os.environ.get("BAR", "1H")                       # candle size
LOOKBACK = 200                                            # candles to pull
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# Trade-plan heuristics (all tunable). None of this is validated against
# real performance — treat as a starting template for your own rules.
LONG_SCORE_MIN = float(os.environ.get("LONG_SCORE_MIN", 62))   # score >= this -> consider long
SHORT_SCORE_MAX = float(os.environ.get("SHORT_SCORE_MAX", 38))  # score <= this -> consider short
ATR_SL_MULT = float(os.environ.get("ATR_SL_MULT", 1.5))         # stop distance = ATR * this
MIN_RR = float(os.environ.get("MIN_RR", 1.5))                    # minimum reward:risk to publish a plan


def fetch_candles(inst_id=INST_ID, bar=BAR, limit=LOOKBACK):
    """OKX returns newest-first: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]"""
    url = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX error: {data}")
    rows = data["data"]
    rows.reverse()  # oldest -> newest
    candles = [
        {
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "vol": float(row[5]),
        }
        for row in rows
    ]
    return candles


def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


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


def suggest_trade_plan(price, score, atr_value, supports, resistances):
    """
    Rule-based entry/SL/TP suggestion. Returns a dict, or None if no setup
    clears the minimum reward:risk bar (mirrors "RR不合格，不開倉" logic).

    Direction is only proposed when the bias score is clearly one-sided.
    Entry favors a pullback toward the nearest support (long) / resistance
    (short) rather than chasing the current price. Stop-loss is ATR-based;
    take-profit targets the next level in that direction.
    """
    if score >= LONG_SCORE_MIN:
        direction = "long"
    elif score <= SHORT_SCORE_MAX:
        direction = "short"
    else:
        return {"direction": None, "reason": "Signal isn't clear enough (score is in the neutral zone) — sitting out this hour."}

    if direction == "long":
        nearest_support = max([s for s in supports if s < price], default=None)
        nearest_resistance = min([r for r in resistances if r > price], default=None)
        # Prefer entering on a pullback to support; fall back to current price
        entry = nearest_support if nearest_support else price
        stop = entry - atr_value * ATR_SL_MULT
        target = nearest_resistance if nearest_resistance else entry + atr_value * ATR_SL_MULT * MIN_RR
        risk = entry - stop
        reward = target - entry
    else:  # short
        nearest_resistance = min([r for r in resistances if r > price], default=None)
        nearest_support = max([s for s in supports if s < price], default=None)
        entry = nearest_resistance if nearest_resistance else price
        stop = entry + atr_value * ATR_SL_MULT
        target = nearest_support if nearest_support else entry - atr_value * ATR_SL_MULT * MIN_RR
        risk = stop - entry
        reward = entry - target

    if risk <= 0 or reward <= 0:
        return {"direction": None, "reason": "Couldn't compute a sane risk:reward — sitting out this hour."}

    rr = reward / risk
    if rr < MIN_RR:
        return {"direction": None, "reason": f"Risk:reward is {rr:.2f}, below the {MIN_RR} threshold — sitting out this hour."}

    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rr": rr,
    }


def build_report(candles):
    closes = [c["close"] for c in candles]
    price = closes[-1]
    r = rsi(closes)
    macd_line, signal_line, hist = macd(closes)
    ema20 = ema(closes, 20)[-1]
    ema50 = ema(closes, 50)[-1] if len(closes) >= 50 else ema(closes, len(closes))[-1]
    supports, resistances = support_resistance(candles)

    trend = "bullish structure" if ema20 > ema50 else "bearish structure"
    momentum = "momentum firming up" if hist > 0 else "momentum fading"

    # Simple heuristic bias score (0-100), NOT a validated win-rate model
    score = 50
    if r is not None:
        score += (r - 50) * 0.4
    score += 15 if ema20 > ema50 else -15
    score += 10 if hist > 0 else -10
    score = max(5, min(95, round(score)))

    nearest_support = max([s for s in supports if s < price], default=supports[0] if supports else None)
    nearest_resistance = min([res for res in resistances if res > price], default=resistances[0] if resistances else None)
    atr_value = atr(candles)
    plan = suggest_trade_plan(price, score, atr_value, supports, resistances)

    lines = []
    lines.append(f"**ETH Hourly Report · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}**")
    lines.append(f"Price: ${price:,.2f}")
    lines.append(f"Trend: {trend}, {momentum}")
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
    return "\n".join(lines)


def post_to_discord(content):
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set — printing report instead:\n")
        print(content)
        return
    resp = requests.post(WEBHOOK_URL, json={"content": content}, timeout=10)
    if resp.status_code >= 300:
        print(f"Discord post failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)


def main():
    candles = fetch_candles()
    if len(candles) < 30:
        print("Not enough candle data returned.", file=sys.stderr)
        sys.exit(1)
    report = build_report(candles)
    post_to_discord(report)


if __name__ == "__main__":
    main()
