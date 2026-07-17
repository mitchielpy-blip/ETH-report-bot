"""
Fill Checker — runs every 15 minutes
--------------------------------------
The main hourly report only evaluates the market once an hour, so a
pullback entry that gets touched mid-hour could be missed until the next
check. This script watches, more frequently, whether the
*already-generated* pending entry has actually been reached, using a
lightweight current-price check instead of a full candle fetch.

When the level IS touched, it re-validates the setup with the same shared
plan logic before confirming the fill (bot.evaluate_plan) — mirroring the
backtest, which discards a pullback whose thesis has decayed by the time it
fills. A setup that has flipped or gone neutral is skipped, the pending order
discarded, and a brief "setup invalidated" heads-up posted, so the live bot
doesn't take trades the backtest would have thrown away (which is what made
the backtest look better than live).

Posts a quick "entry filled" alert to Discord the moment a still-valid fill
happens, instead of waiting for the next hourly report to notice.
"""

import sys
import os
from datetime import datetime, timezone

import eth_report_bot as bot

PENDING_ORDER_EXPIRY_HOURS = float(os.environ.get("PENDING_ORDER_EXPIRY_HOURS", 8))


def main():
    state = bot.load_state()
    direction = state.get("direction")

    if not direction:
        print("No pending signal right now — nothing to watch.")
        return

    if state.get("filled"):
        print("Pending signal was already marked filled — nothing new to check.")
        return

    generated_at_ts = state.get("generated_at_ts")
    if generated_at_ts:
        age_hours = (datetime.now(timezone.utc).timestamp() * 1000 - generated_at_ts) / (3600 * 1000)
        if age_hours > PENDING_ORDER_EXPIRY_HOURS:
            print(f"Pending {direction} signal is {age_hours:.1f}h old — treating as expired, no longer watching.")
            state["direction"] = None
            bot.save_state(state)
            return

    entry = state.get("entry")
    if entry is None:
        print("Pending signal is missing an entry price — nothing to check.")
        return

    price = bot.fetch_ticker_price()
    print(f"Current price: ${price:,.2f} | Watching {direction} entry at ${entry:,.2f}")

    filled = (direction == "long" and price <= entry) or (direction == "short" and price >= entry)
    if not filled:
        print("Entry not reached yet.")
        return

    # Re-validate the thesis at fill time, exactly as the backtest does before
    # it counts a pullback fill as a real trade (backtest.simulate_trade's
    # fill-time re-check). A pullback entry can take hours to actually get
    # touched; if the setup has flipped or decayed to neutral by now, the
    # original plan is stale and the live bot must skip it too. Without this,
    # the backtest — which discards these "invalidated" fills — silently
    # overstates performance versus what the live bot actually trades.
    #
    # Parity note: the backtest re-checks on the exact 1H fill bar, whereas
    # here we can only evaluate the most recent *completed* candles at the
    # moment the ticker crosses the level (fills are watched intra-hour). That
    # residual granularity gap can't be closed without reconstructing the
    # in-progress bar, but re-checking on the latest closed bars is far closer
    # to the backtest than filling blindly. We pass the pending direction as
    # previous_raw_direction so the re-check only asks "has the raw signal
    # flipped?", not "re-confirm over two fresh hours" — matching the backtest.
    if bot.STRATEGY == "price_action":
        # Retest-aware re-check: the BOS/rejection already happened before this
        # retest fill, so re-running the full 4-step chain would reject every
        # genuine retest. revalidate_fill only asks whether the 4H trend still
        # agrees (see price_action.revalidate_fill). We fetch the four
        # timeframes only now that the level has actually been touched, keeping
        # the common "not filled yet" path a single cheap ticker call.
        fresh = bot._price_action_revalidate(bot.fetch_timeframes(), direction)
    else:
        # previous_raw_streak = PERSIST_HOURS - 1 so the persistence gate reduces
        # to a pure "does the raw signal still agree?" check: if it does, the
        # streak reaches the threshold and passes; if it has flipped/gone
        # neutral, it resets and fails — which is exactly the invalidation we
        # want here (not a fresh multi-hour re-confirmation).
        fresh = bot.evaluate_plan(bot.fetch_candles(), previous_raw_direction=direction,
                                  previous_raw_streak=max(bot.PERSIST_HOURS - 1, 0))
    if not fresh or fresh.get("direction") != direction:
        if not fresh:
            reason = "insufficient data to re-evaluate"
        elif fresh.get("direction"):
            reason = f"signal has flipped to {fresh['direction']}"
        else:
            reason = fresh.get("reason") or "signal no longer clear enough"
        print(f"{direction.upper()} entry at ${entry:,.2f} was touched, but the setup no "
              f"longer holds ({reason}) — skipping the fill and discarding the pending "
              f"order, matching the backtest's invalidation of stale pullbacks.")
        bot.post_to_discord(
            f"**Setup invalidated** · {direction.upper()} @ ${entry:,.2f} was touched, "
            f"but the signal no longer holds ({reason}).\n"
            f"Standing aside — no trade. _The pullback level from the last hourly report "
            f"was reached, but the setup broke down before fill, so the bot is skipping it "
            f"rather than entering a stale trade._"
        )
        state["direction"] = None
        bot.save_state(state)
        return

    message = (
        f"**Entry filled** · {direction.upper()} @ ${price:,.2f}\n"
        f"Planned entry: ${entry:,.2f}  |  Stop: ${state.get('stop', 0):,.2f}  |  Target: ${state.get('target', 0):,.2f}\n"
        f"_This is just confirming the pullback level from the last hourly report was reached — not a new signal._"
    )
    bot.post_to_discord(message)

    state["filled"] = True
    bot.save_state(state)
    print("Marked as filled and posted alert.")


if __name__ == "__main__":
    main()
