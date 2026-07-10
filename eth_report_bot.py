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


def build_report(candles):
    closes = [c["close"] for c in candles]
    price = closes[-1]
    r = rsi(closes)
    macd_line, signal_line, hist = macd(closes)
    ema20 = ema(closes, 20)[-1]
    ema50 = ema(closes, 50)[-1] if len(closes) >= 50 else ema(closes, len(closes))[-1]
    supports, resistances = support_resistance(candles)

    trend = "多頭排列" if ema20 > ema50 else "空頭排列"
    momentum = "動能偏強" if hist > 0 else "動能偏弱"

    # Simple heuristic bias score (0-100), NOT a validated win-rate model
    score = 50
    if r is not None:
        score += (r - 50) * 0.4
    score += 15 if ema20 > ema50 else -15
    score += 10 if hist > 0 else -10
    score = max(5, min(95, round(score)))

    nearest_support = max([s for s in supports if s < price], default=supports[0] if supports else None)
    nearest_resistance = min([res for res in resistances if res > price], default=resistances[0] if resistances else None)

    lines = []
    lines.append(f"**ETH 小時報 · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}**")
    lines.append(f"價格：${price:,.2f}")
    lines.append(f"趨勢：{trend}，{momentum}")
    lines.append(f"RSI(14)：{r:.1f}" if r else "RSI：資料不足")
    lines.append(f"MACD 柱：{hist:+.2f}")
    lines.append(f"偏多評分：{score}/100（僅供參考，非勝率）")
    if supports:
        lines.append(f"關鍵支撐：{', '.join(f'{s:,.0f}' for s in supports)}")
    if resistances:
        lines.append(f"關鍵阻力：{', '.join(f'{rr:,.0f}' for rr in resistances)}")
    if nearest_support and nearest_resistance:
        lines.append(f"最近區間：{nearest_support:,.0f} - {nearest_resistance:,.0f}")
    lines.append("\n_僅為技術指標自動彙整，不構成投資建議，請自行判斷風險。_")
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
