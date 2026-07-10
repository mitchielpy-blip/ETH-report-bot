# ETH Hourly Report Bot

Posts an automated ETH technical-analysis report to Discord every hour,
using free public price data from OKX.

**Not financial advice.** The "偏多評分" (bias score) is a simple heuristic
built from RSI/EMA/MACD — not a validated win-rate model. Treat it as a
quick glance, not a signal to trade on.

## What you get in the report
- Current price
- Trend (EMA20 vs EMA50) + momentum (MACD histogram) on the 1H timeframe
- A 4H higher-timeframe trend check — trade suggestions that would fight
  the bigger trend are automatically suppressed
- RSI(14)
- A rough 0–100 bias score
- Clustered support/resistance levels from the last 40 hourly candles
- **A rule-based entry / stop-loss / take-profit suggestion**, but only
  when the setup clears a minimum reward:risk bar AND agrees with the
  4H trend — otherwise it tells you it's sitting out

### Reliability
- Network calls to OKX and Discord retry automatically (up to 3 attempts
  with backoff) instead of silently failing on a hiccup.
- Consecutive "no entry" hours are suppressed so a choppy market doesn't
  spam the channel every hour — any active or newly-changed signal always
  posts. This works by committing a small `state.json` file back to the
  repo after each run (that's why the workflow needs `contents: write`
  permission and a commit/push step — already included).

### How the trade plan is built (all tunable via env vars)
- **Direction**: only proposed when the bias score is clearly one-sided
  (`LONG_SCORE_MIN` / `SHORT_SCORE_MAX`, default 62 / 38). Middling scores
  → no plan.
- **Higher-timeframe filter**: if the 4H trend disagrees with the proposed
  direction, the plan is dropped (change with `HTF_BAR`, default `4H`).
- **Entry**: a pullback to the nearest support (long) / resistance (short)
  rather than chasing the current price.
- **Stop-loss**: ATR(14) × `ATR_SL_MULT` (default 1.5) beyond entry — scales
  with current volatility instead of a fixed dollar amount.
- **Take-profit**: the next support/resistance level in that direction.
- **Gate**: if the resulting reward:risk is below `MIN_RR` (default 1.5),
  no plan is published — you just get the reason why.

This is a rule template, not a backtested strategy. Adjust the thresholds
to match how your own pullback method actually behaves before trusting it.

## Setup (free, runs on GitHub — no server needed)

1. **Create a Discord webhook**
   - In Discord: Server Settings → Integrations → Webhooks → New Webhook
   - Pick the channel you want reports posted to, copy the Webhook URL

2. **Create a new GitHub repo** (private is fine) and upload these files:
   - `eth_report_bot.py`
   - `requirements.txt`
   - `.github/workflows/eth-report.yml`

3. **Add your webhook as a secret**
   - Repo → Settings → Secrets and variables → Actions → New repository secret
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: (paste the webhook URL)

4. **Done.** GitHub will run the workflow every hour automatically (the
   `cron: "0 * * * *"` line in the workflow file). You can also trigger it
   manually anytime from the repo's Actions tab ("Run workflow").

## Customizing

- Change `INST_ID` env var if you want spot (`ETH-USDT`) instead of the
  perpetual swap (`ETH-USDT-SWAP`, the default).
- Change `BAR` (e.g. `15m`, `4H`) to change the candle timeframe used
  for analysis — this is independent of how often the report posts.
- To change *how often* it posts, edit the cron schedule in
  `.github/workflows/eth-report.yml` (cron is in UTC).

## Running locally to test

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python eth_report_bot.py
```

If `DISCORD_WEBHOOK_URL` isn't set, it just prints the report to your
terminal instead of posting — useful for testing changes.
