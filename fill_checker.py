"""
Fill Checker — runs every 15 minutes
--------------------------------------
The main hourly report only evaluates the market once an hour, so a
pullback entry that gets touched mid-hour could be missed until the next
check. This script doesn't change the signal logic at all (the validated
1H-based analysis stays exactly as-is) — it just watches, more frequently,
whether the *already-generated* pending entry has actually been reached,
using a lightweight current-price check instead of a full candle fetch.

Posts a quick "entry filled" alert to Discord the moment it happens,
instead of waiting for the next hourly report to notice.
"""

import sys
import os
from datetime import datetime, timezone

import eth_report_bot as bot

PENDING_ORDER_EXPIRY_HOURS = float(os.environ.get("PENDING_ORDER_EXPIRY_HOURS", 24))


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
