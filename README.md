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

> **BTC added 2026-07-17.** `BTC-USDT-SWAP` now runs as a third instrument
> alongside ETH and SOL (same indicator strategy, `ADX_MIN` 20, own
> `state_btc.json` / `signals_log_btc.csv`, posts to the same Discord
> channel under the name **"BTC Hourly Report"**). This **reverses the
> 2026-07-14 rejection below**, and here's why: that call rested on a
> *single* 12-month window that happened to land on a soft patch. Re-tested
> across three independent windows at `ADX_MIN` 20, BTC is net-positive in
> every one and never had a losing full window:
> - Recent 12mo (2025-07→2026-07): 66 trades, **53.1% win rate**, +0.25R net
>   — but halves −0.04R / +0.55R (win-rate halves 41.9% / 63.6%; all the edge
>   in the back half). Re-run 2026-07-17; max drawdown 8.1%.
> - Prior 12mo (2024-07→2025-07): 52 trades, +0.23R net, halves
>   +0.29R / +0.17R — **both positive, passes the split cleanly**.
> - 18mo (2025-01→2026-07): 101 trades, +0.19R net, halves +0.03R / +0.34R.
>
> The weak first halves all fall in a ~H2-2025 regime; outside it BTC trades
> like ETH/SOL. It's a touch less walk-forward-consistent than the other two,
> so treat that flat patch as an *expected* stretch, not a failure — and size
> it no larger than ETH/SOL. Price-action on BTC was re-checked and stays
> **dead** (only ~9 fully-aligned setups in a year, 0 wins), so BTC runs the
> indicator strategy only. Raising `ADX_MIN` (25/30) only hurt BTC, so the
> default 20 is kept.
>
> **BTC session filter (added 2026-07-18, live, BTC-only).** BTC now runs with
> `SKIP_SESSIONS=asia` — it sits out signals generated in the Asia session
> (00–08 UTC). `diagnostics.py` showed BTC's Asia session was a persistent drag
> across *two independent* 12-month windows (−0.10R and −0.08R avg, ~39% win,
> N≈32 each), while Europe/US carried the edge — and the full walk-forward
> backtest confirmed it: skipping Asia lifts BTC from **47.9% / +0.14R / 8.0% DD**
> to **49.6% / +0.19R / 6.4% DD** (144→130 trades) and turns the weak first half
> (−0.02R) positive (+0.05R). The same filter is **neutral-to-harmful on ETH and
> SOL** (Asia isn't a drag for them — it just trims trades, and slightly *raises*
> SOL's drawdown), so it stays BTC-only. A textbook case for per-instrument, not
> global, tuning.
>
> **All-three snapshot, same trailing-12mo window — after the 2026-07-17
> fill-gate fix:** the fill-time re-check no longer re-gates R:R against
> freshly-recomputed levels (it only checks the direction hasn't flipped),
> which roughly *doubled* the number of fills. On one consistent recent
> window: **SOL 55.0% win / +0.35R** (121 trades, sim equity +50%, 5.1% DD),
> **ETH 50.0% win / +0.23R** (120 trades, +31%, 12.1% DD), **BTC 47.9% win /
> +0.14R** (144 trades, +21%, 8.0% DD). All three keep both walk-forward
> halves positive (BTC's first half is ~flat). This trades a higher hit rate
> for ~2x the activity: the old R:R re-gate was inadvertently filtering out
> fills into expanded-volatility / no-room-to-target conditions, so removing
> it recovers those setups but dilutes per-trade edge while total return stays
> positive. **These supersede the earlier pre-fix figures** — SOL 63.2% /
> +0.56R (68 trades), ETH 55.7% / +0.39R (61), BTC 53.1% / +0.25R (66) — that
> the per-window BTC analysis above still quotes. BTC is the weakest on every
> axis. All three shared the same weak-first-half / strong-second-half shape
> this window (they're correlated), so these trailing numbers are flattered by
> the back half — don't read the headline win rate as a steady-state rate.
>
> **SOL added + short gate loosened 2026-07-14.** Two changes, both
> validated on 12-month 1H backtests before going live:
> 1. **SOL-USDT-SWAP now runs as a second instrument** alongside ETH (same
>    strategy, own `state_sol.json` / `signals_log_sol.csv`). SOL's
>    12-month baseline: 40 trades, 56.4% win rate, +0.371R/trade net,
>    3.3% max drawdown, both half-window splits positive (+0.23R/+0.51R).
>    BTC was tested at the same time and REJECTED: +0.216R overall but all
>    of it from one half of the year (−0.23R / +0.67R splits).
> 2. **`SHORT_SCORE_MAX` 38 → 45** via the parameter sweep: 51 trades
>    (vs 46), +0.330R/trade (vs +0.314R), identical 5.0% max drawdown, and
>    perfectly consistent halves (+0.330R/+0.330R). Loosening the long gate
>    (`LONG_SCORE_MIN`) was tested too and made things worse — left at 62.
>    Note this is in-sample; treat the gain as noise-level and the change
>    as "not harmful, slightly more active".
>
> **New ETH baseline to compare live results against:** 51 trades/12mo,
> 52.9% win rate, +0.330R net expectancy. Combined with SOL that's ~90
> fills/year — roughly one filled trade every 4 days across the two
> instruments (they're correlated, so expect losing streaks to overlap).
> Prior `signals_log.csv` archived as `signals_log_pre_v3.csv`, since the
> short-gate change alters which ETH signals publish.
>
> **Fill-time re-validation added 2026-07-12.** `fill_checker.py` now
> re-checks the setup with the shared plan logic (`evaluate_plan`) the
> moment a pending pullback entry is touched, and *skips* the fill if the
> signal has flipped or decayed to neutral by then — exactly as the
> backtest already discarded those "invalidated" pullbacks. Previously the
> backtest threw these trades away but the live bot took them, so the
> backtest's expectancy was optimistically biased. This changes which
> fills go live, so treat pre-2026-07-12 fills as a slightly different
> (more permissive) strategy when reading `forward_test_report.py`.
>
> **Refined 2026-07-17.** The fill-time re-check now only asks whether the
> *direction* still holds — it no longer re-applies the `MIN_RR` gate to a
> plan rebuilt from the latest ATR. A pending order already has its
> entry/stop/target (and R:R) locked from signal time, so re-gating on a
> freshly-recomputed R:R was discarding valid fills purely because volatility
> had ticked up before fill. The direction-flip protection stays; only the
> spurious R:R invalidation is gone (`require_rr=False` at fill time, shared
> by live and backtest). This roughly doubled fills — see the post-fix
> all-three snapshot above for the win-rate/expectancy impact.
>
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
>
> **Known ways the backtest can still flatter live** (so treat the baseline
> as a ceiling, not a promise):
> 1. *Fill granularity* — the backtest fills on any intra-bar wick to the
>    entry, but `fill_checker.py` only samples the price every 15 minutes, so
>    a quick spike to the level and back can fill in the backtest yet be
>    missed live. The backtest's fill rate is therefore an over-estimate.
> 2. *Stop/target slippage* — the backtest assumes stops and targets execute
>    exactly at their price (it charges fees but no slippage). Real stops slip
>    in fast moves, which mostly hurts losers, so net R is mildly optimistic.
> 3. *In-sample tuning* — `PULLBACK_ATR_MULT=0.7` was tuned on the same window
>    this baseline reports. Re-run with `--end-date` on an earlier, untouched
>    window for a truer out-of-sample read before trusting it.

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
- The report only posts when the recommendation actually **changes** from
  the last thing posted — a fresh signal (No-entry → long/short), a flip
  (long ↔ short), or a stand-down (long/short → No-entry). A one-sided bias
  can hold for many hours and the indicator re-derives an almost-identical
  plan each run (only the ATR/price-based entry drifts a few cents); without
  this, a standing long/short would ping the channel every hour. This works
  by committing a small `state.json` file back to the repo after each run
  (that's why the workflow needs `contents: write` permission and a
  commit/push step — already included). The price-action strategy keeps its
  own zone-based dedup instead (two distinct zones sharing a direction are
  different trades and each posts).
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
- **Session filter** (opt-in, off by default): `SKIP_SESSIONS` is a
  comma-separated list of sessions to sit out, keyed by the signal bar's UTC
  hour — `asia` (00–08 UTC / 08–16 SGT), `europe` (08–16 UTC), `us` (16–24
  UTC), the same buckets `diagnostics.py` breaks results down by. Blank (the
  default) trades every session, so the validated model is unchanged unless you
  opt in. It's a signal-*generation* gate: a pending order created in an allowed
  session still fills normally even if its entry is touched during a filtered
  one. Set per-instrument when diagnostics show a session is a persistent,
  *out-of-sample* drag — e.g. BTC's Asia session lost across two independent
  12-month windows (−0.10R and −0.08R avg), so `SKIP_SESSIONS=asia` is a
  candidate there. Confirm with a full backtest before relying on it.
- **Entry**: a volatility-scaled pullback (`PULLBACK_ATR_MULT` × ATR from
  current price, default 0.7 — see forward-test log above for why),
  capped at the nearest support/resistance level if closer.
- **Stop-loss**: ATR(14) × `ATR_SL_MULT` (default 1.5) beyond entry — scales
  with current volatility instead of a fixed dollar amount.
- **Take-profit**: the next support/resistance level in that direction.
- **Gate**: if the resulting reward:risk is below `MIN_RR` (default 1.5),
  no plan is published — you just get the reason why.
- **Exit management** (`EXIT_MODEL`, default `fixed` — **backtest-research only,
  not yet live**): `fixed` is set-and-forget — a filled trade runs to its
  original stop, its target, or the hold timeout, which is exactly what the live
  bot delivers today (the report posts entry/stop/target, `fill_checker.py`
  confirms the fill, and nothing manages the position after that). `breakeven`
  slides the stop up to entry once the trade has gone `BREAKEVEN_AT_R` (default
  1.0) in favour — turning a pullback into a scratch instead of a loss (a new
  `breakeven` outcome in the backtest summary, counted as ~0R and excluded from
  the win-rate denominator like a timeout). `trailing` trails the stop
  `TRAIL_DISTANCE_R` (default 1.0) behind the best price once the trade clears
  `TRAIL_AT_R` (default 1.0), and by default lets the winner run *past* the fixed
  target (`TRAIL_HONOR_TARGET=false`; set true to keep the target as a hard cap
  and use the trail only for downside protection). Any stop move is applied
  *after* each bar's exit check, so the bar that first reaches the trigger can
  still be stopped at the original level — no intrabar lookahead. Exit logic lives
  in one pure stepper (`exit_manager.ManagedExit`) so the backtest and a future
  live position-manager can decide exits identically. **`EXIT_MODEL` must stay
  `fixed` on every live workflow** until that live position-manager exists —
  otherwise the backtest would model a stop move the live alerts never tell you to
  make (a phantom edge). Only `backtest.py` reads it. Set it for a backtest via
  the `exit_model` input on the Run Backtest workflow.

  _Measured (12-month walk-forward, matched to each instrument's live config).
  Net expectancy (R/trade) — the decision metric — for every exit variant:_

  | Instrument | `fixed` | `breakeven` @1R | `trailing` ride-past | `trailing` +target-cap |
  |------------|---------|-----------------|----------------------|------------------------|
  | ETH | **+0.23R** (50.0%) | +0.20R | +0.17R (56.9%) | +0.16R (58.2%) |
  | SOL | **+0.34R** (54.5%) | +0.27R | +0.27R (60.5%) | +0.26R (61.7%) |
  | BTC | **+0.19R** (49.6%) | +0.12R | +0.17R (63.2%) | +0.11R (65.5%) |

  _Conclusion: **no managed-exit variant beats `fixed` on net expectancy on any
  instrument** — `fixed` set-and-forget wins net R across the board. `breakeven`
  scratches the retrace-then-run trades the edge relies on (on BTC it turned 30%
  of trades into breakeven scratches). Both `trailing` variants buy a large
  win-rate jump (+8 to +16pp) and smoother drawdowns, but cost ~0.05–0.08R of
  expectancy every time — a risk-profile reshaping, not an edge. The
  `TRAIL_HONOR_TARGET=true` (target-cap) hypothesis specifically failed: capping
  winners made BTC **worse** than plain ride-past trailing (+0.11R vs +0.17R),
  because BTC's edge lives in a fat right tail of trades that run past the fixed
  target — capping them amputates exactly the trades that pay for the strategy.
  So `fixed` stays the default and the only exit the bot actually delivers.
  `EXIT_MODEL` remains a backtest-research knob; nothing here is wired live._

This is a rule template, backed by `backtest.py` and `backtest_sweep.py`
so changes can be checked against history before going live — but no
amount of backtesting guarantees future performance. Treat every
parameter as provisional, not settled.

## Choosing a strategy (`STRATEGY` env var)

The bot ships with two independent strategies. Pick one with the `STRATEGY`
environment variable — everything else (report format, Discord posting,
`state.json`, `signals_log.csv`, `fill_checker.py`, `backtest.py`) works the
same either way.

- **`STRATEGY=indicator`** (default) — the indicator bias-score model described
  above (RSI/MACD/EMA/ADX/volume on 1H + a 4H EMA trend filter). This is the
  forward-tested one (see the log at the top of this file).
- **`STRATEGY=price_action`** — a 4-timeframe structure/zone/rejection model
  (implemented in `price_action.py`), a mechanical version of a discretionary
  price-action checklist. **Not yet validated — backtest it before relying on
  it.**

### The price-action strategy (4 questions before an entry)

1. **Trend — 4H.** Swing structure: higher-highs *and* higher-lows → longs
   only; lower-highs *and* lower-lows → shorts only; anything else (ranging) →
   stand aside.
2. **Zone — 1H.** The last swing point price left *aggressively* — an impulse
   of at least `PA_IMPULSE_ATR_MULT` × ATR within `PA_IMPULSE_MAX_BARS` bars.
   That candle's range is the reversal ("where big money entered") zone.
3. **Reaction — 15M.** Wait for price to tag the zone and *reject* it (a wick
   into the zone making up ≥ `PA_REJECTION_WICK_RATIO` of the candle, closing
   back out). No reaction = no trade.
4. **Confirmation — 5M.** Wait for a break of the opposite side's structure —
   for a long, a 5M *close* above the last lower-high; for a short, a close
   below the last higher-low.

Only when all four align is a plan produced: **entry** at the broken structure
level (a retest, so it fits the same pending-order fill model as the indicator
strategy), **stop** just beyond the zone (`PA_ZONE_STOP_ATR_MULT` × 5M ATR
buffer), **target** the next opposing 1H swing, gated by the same `MIN_RR`.

The discretionary language above is turned into these tunables (all env vars,
all sweepable like `PULLBACK_ATR_MULT`):

| Env var | Default | Meaning |
|---|---|---|
| `PA_SWING_LEFT` / `PA_SWING_RIGHT` | 2 / 2 | pivot bars each side for swing detection |
| `PA_IMPULSE_ATR_MULT` | 2.0 | how far price must leave the zone (in 1H ATRs) to count as "aggressive" |
| `PA_IMPULSE_MAX_BARS` | 5 | …within this many 1H bars |
| `PA_REJECTION_LOOKBACK` | 8 | 15M bars to look back for a rejection |
| `PA_REJECTION_WICK_RATIO` | 0.5 | min wick share of the rejection candle |
| `PA_ZONE_STOP_ATR_MULT` | 0.5 | stop buffer beyond the zone, in 5M ATRs |
| `PA_BOS_LEFT` / `PA_BOS_RIGHT` | 1 / 1 | pivot bars each side for the 5M break-of-structure check |

Because the 5M break-of-structure trigger is time-sensitive, the price-action
strategy runs on its **own workflow** (`.github/workflows/price-action.yml`,
every 15 minutes) with its own `state_pa*.json` / `signals_log_pa*.csv` files,
so it never clobbers the hourly indicator strategy's state. `fill_checker.py`
also watches the price-action pending entries (added to
`.github/workflows/fill-checker.yml`) and posts an **"Entry filled"** alert
when the retest level is touched — after re-checking that the 4H trend still
agrees, so a setup that went stale while waiting for the retest is skipped
rather than entered.

### Backtesting the price-action strategy

`backtest.py` reads the same `STRATEGY` env var. For `price_action` it fetches a
single **5M** history feed and resamples it up to 15m/1H/4H (mirroring how the
live bot fetches those four timeframes), then walks forward 5M-bar-by-5M-bar
with no lookahead:

```bash
STRATEGY=price_action python backtest.py --months 6      # base bar is fixed at 5m
```

It prints the same summary (win rate / expectancy / drawdown / first-vs-second-
half split) and `trades.csv` as the indicator backtest. Tune the 5M fill/hold
windows with `PA_ENTRY_WAIT_BARS` (default 24 = ~2h) and `PA_MAX_HOLD_BARS`
(default 288 = ~24h).

## Setup (free, runs on GitHub — no server needed)

1. **Create a Discord webhook**
   - In Discord: Server Settings → Integrations → Webhooks → New Webhook
   - Pick the channel you want reports posted to, copy the Webhook URL

2. **Create a new GitHub repo** (private is fine) and upload these files:
   - `eth_report_bot.py`
   - `price_action.py` (the `STRATEGY=price_action` strategy core)
   - `backtest.py`
   - `fill_checker.py`
   - `forward_test_report.py`
   - `backtest_sweep.py` (optional — only needed if re-tuning parameters)
   - `requirements.txt`
   - `.github/workflows/eth-report.yml`
   - `.github/workflows/price-action.yml` (optional — only if running `STRATEGY=price_action`)
   - `.github/workflows/fill-checker.yml`
   - `.github/workflows/backtest.yml`
   - `.github/workflows/pullback-sweep.yml` (optional, pairs with the sweep script)

3. **Add your webhook as a secret**
   - Repo → Settings → Secrets and variables → Actions → New repository secret
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: (paste the webhook URL)

4. **If OKX geo-blocks GitHub's runners, add a proxy secret** (`OKX_PROXY`)
   - GitHub-hosted runners run from US/Azure IPs. OKX geo-restricts those and
     answers the API with an **HTTP 307 redirect** instead of price data, so
     the scheduled jobs fail even though the same code works on your machine.
   - The fix is to route *only* the OKX calls through a proxy in a region OKX
     serves (Discord posts and the state commit stay direct):
     - Repo → Settings → Secrets and variables → Actions → New repository secret
     - Name: `OKX_PROXY`
     - Value: your proxy URL, e.g. `http://user:pass@host:port` (or `socks5h://…`
       — if you use a SOCKS proxy, add `requests[socks]` to `requirements.txt`).
   - Leave `OKX_PROXY` unset when running locally from an allowed region; the
     bot calls OKX directly in that case. If a run ever fails with the "OKX
     redirected the request … Set the OKX_PROXY secret" error, this is why.

5. **Done.** GitHub will run the workflow every hour automatically (the
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

# Compare pullback-entry depth options
python backtest_sweep.py --months 12 --values 1.0,0.7,0.5,0.3

# ...or sweep any other strategy threshold, e.g. the reward:risk gate
python backtest_sweep.py --months 12 --param MIN_RR --values 1.5,1.4,1.3,1.2

# After enough live signals have accumulated and resolved:
python forward_test_report.py
```
