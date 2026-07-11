# ETH Hourly Report Bot

Posts an automated ETH technical-analysis report to Discord every hour,
using free public price data from OKX.

**Not financial advice.** The "偏多評分" (bias score) is a simple heuristic
built from RSI/EMA/MACD — not a validated win-rate model. Treat it as a
quick glance, not a signal to trade on.

## Forward-test log

Keep this section up to date whenever a real bug fix or parameter change
goes live — it's the reference point for whether `forward_test_report.py`'s
numbers are even measuring the strategy you think they are.

> **Restarted 2026-07-11.** Prior signals_log.csv archived as
> `signals_log_pre_v2.csv` (not deleted — kept for reference, but excluded
> from forward-test comparisons since it predates the fixes below).
>
> Changes since the previous forward-test window:
> 1. **Confirmed-candle fix** — `fetch_candles()` now discards OKX's
>    in-progress candle. Previously every hourly run computed indicators
>    (including `volume_ratio`) on a candle only minutes old, which
>    understated volume and made live behavior diverge from the backtest.
> 2. **HTF boundary alignment fix** — `backtest.py`'s `resample_htf` now
>    anchors 4H buckets to real UTC boundaries instead of shifting every
>    hour, so the backtest's HTF filter finally matches what the live bot
>    actually fetches from OKX.
> 3. **`PULLBACK_ATR_MULT` 1.0 → 0.7** — chosen via `backtest_sweep.py`
>    over a 12-month 1H window. Raised fill rate from 11.1% → 20.3% while
>    *improving* net expectancy (0.145R → 0.314R/trade) and win rate
>    (45.5% → 52.2%), with both half-window splits solidly positive
>    (+0.335R / +0.293R). Full comparison across 1.0/0.7/0.5/0.3 saved in
>    `pullback_sweep.csv` from that run.
>
> **Backtest baseline to compare live results against:** 52.2% win rate,
> +0.314R net expectancy per trade, 20.3% fill rate, 12-month 1H window.
> Wait for a reasonable sample (15–20+ resolved trades) before drawing
> conclusions from `forward_test_report.py` — same logic as the backtest's
> own first-half/second-half split check.

## What you get in the report
- Current price (the last **completed** 1H candle's close — not the
  live in-progress price, by design; see the confirmed-candle fix above)
- Trend (EMA20 vs EMA50) + momentum (MACD histogram) on the 1H timeframe
- A 4H higher-timeframe trend check — trade suggestions that would fight
  the bigger trend are automatically suppressed
- RSI(14)
- ATR(14) and ADX(14) — volatility and trend-strength filters
- Volume confirmation relative to a 20-candle average
- A rough 0–100 bias score
- Clustered support/resistance levels from the last 40 hourly candles
- **A rule-based entry / stop-loss / take-profit suggestion**, but only
  when the setup clears a minimum reward:risk bar AND agrees with the
  4H trend AND the market isn't flat/choppy (ADX) — otherwise it tells
  you it's sitting out

### Reliability
- Network calls to OKX and Discord retry automatically (up to 3 attempts
  with backoff) instead of silently failing on a hiccup.
- Consecutive "no entry" hours are suppressed so a choppy market doesn't
  spam the channel every hour — any active or newly-changed signal always
  posts. This works by committing a small `state.json` file back to the
  repo after each run (that's why the workflow needs `contents: write`
  permission and a commit/push step — already included).
- A still-pending (unfilled) entry is preserved across "no entry" hours
  for up to `PENDING_ENTRY_LIFETIME_HOURS` (default 8h) instead of being
  wiped by the next hourly run, so `fill_checker.py` keeps watching it
  for its full intended window.

### How the trade plan is built (all tunable via env vars)
- **Direction**: only proposed when the bias score is clearly one-sided
  (`LONG_SCORE_MIN` / `SHORT_SCORE_MAX`, default 62 / 38). Middling scores
  → no plan. Also requires the same raw direction to persist for two
  consecutive hours before acting, as a noise filter.
- **Higher-timeframe filter**: if the 4H trend disagrees with the proposed
  direction, the plan is dropped (change with `HTF_BAR`, default `4H`).
- **Trend-strength filter**: if ADX(14) is below `ADX_MIN` (default 20),
  the market is treated as flat/choppy and no plan is proposed.
- **Entry**: a volatility-scaled pullback (`PULLBACK_ATR_MULT` × ATR from
  current price, default 0.7 — see forward-test log above for why),
  capped at the nearest support/resistance level if closer.
- **Stop-loss**: ATR(14) × `ATR_SL_MULT` (default 1.5) beyond entry — scales
  with current volatility instead of a fixed dollar amount.
- **Take-profit**: the next support/resistance level in that direction.
- **Gate**: if the resulting reward:risk is below `MIN_RR` (default 1.5),
  no plan is published — you just get the reason why.

This is a rule template, backed by `backtest.py` and `backtest_sweep.py`
so changes can be checked against history before going live — but no
amount of backtesting guarantees future performance. Treat every
parameter as provisional, not settled.

## Setup (free, runs on GitHub — no server needed)

1. **Create a Discord webhook**
   - In Discord: Server Settings → Integrations → Webhooks → New Webhook
   - Pick the channel you want reports posted to, copy the Webhook URL

2. **Create a new GitHub repo** (private is fine) and upload these files:
   - `eth_report_bot.py`
   - `backtest.py`
   - `fill_checker.py`
   - `forward_test_report.py`
   - `backtest_sweep.py` (optional — only needed if re-tuning parameters)
   - `requirements.txt`
   - `.github/workflows/eth-report.yml`
   - `.github/workflows/fill-checker.yml`
   - `.github/workflows/backtest.yml`
   - `.github/workflows/pullback-sweep.yml` (optional, pairs with the sweep script)

3. **Add your webhook as a secret**
   - Repo → Settings → Secrets and variables → Actions → New repository secret
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: (paste the webhook URL)

4. **Done.** GitHub will run the workflow every hour automatically (the
   `cron: "7 * * * *"` line in the workflow file). You can also trigger it
   manually anytime from the repo's Actions tab ("Run workflow").

## Customizing

- Change `INST_ID` env var if you want spot (`ETH-USDT`) instead of the
  perpetual swap (`ETH-USDT-SWAP`, the default).
- Change `BAR` (e.g. `15m`, `4H`) to change the candle timeframe used
  for analysis — this is independent of how often the report posts.
  **Note:** the current indicator thresholds (score cutoffs, ADX_MIN,
  ATR multipliers) were tuned against 1H data. Switching `BAR` is a real
  strategy change, not a schedule tweak — re-run `backtest.py` on the new
  timeframe before trusting it live.
- To change *how often the hourly report posts*, edit the cron schedule
  in `.github/workflows/eth-report.yml` (cron is in UTC). Running the
  full report more often than once an hour won't surface new information
  — 1H indicators only change when a new 1H candle closes. For faster
  reaction to price between hourly reports, `fill_checker.py` already
  runs every 15 minutes to watch for pending-entry fills using a cheap
  ticker check, without re-running the full analysis.
- Any time a parameter like `PULLBACK_ATR_MULT`, `ADX_MIN`, `MIN_RR`, etc.
  is changed based on new backtest/sweep results, update the **forward-test
  log** section above with the date and new baseline, and archive
  `signals_log.csv` (rename it, don't delete it) so forward-test
  comparisons only reflect the current version of the strategy.

## Running locally to test

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python eth_report_bot.py
```

If `DISCORD_WEBHOOK_URL` isn't set, it just prints the report to your
terminal instead of posting — useful for testing changes.

### Validating changes before going live

```bash
# Full backtest against the current live logic
python backtest.py --months 12

# Compare pullback-entry depth options (or any other parameter you
# want to sweep — edit backtest_sweep.py's target env var if needed)
python backtest_sweep.py --months 12 --values 1.0,0.7,0.5,0.3

# After enough live signals have accumulated and resolved:
python forward_test_report.py
```
