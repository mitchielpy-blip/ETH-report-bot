"""
Exit management — the shared, pure decision half of "how a filled trade is
managed", mirroring how evaluate_plan is the shared decision half of "what
trade to take".

The models are "fixed" (set-and-forget: the trade runs to its original stop, its
target, or the caller's hold timeout), "breakeven" (once price has gone far
enough in favour, slide the stop up to entry so the trade can no longer lose),
and "trailing" (once price clears an activation threshold, trail the stop a fixed
R behind the best price and let the winner run past the target). The class is
deliberately a *stepper* fed one bar at a time and holding no
network or config of its own, so the same instance logic can drive both:

  * backtest.simulate_trade — fed every candle from fill to exit, and
  * a future live position-manager — fed the latest completed bar each run,

from ONE implementation. If the backtest managed exits one way and the live
alerts another, the backtest would stop describing the bot — the same parity
rule that keeps evaluate_plan the single entry path. Everything is expressed in
R (favourable excursion vs be_at_r * risk), so it's unit- and instrument-
agnostic: the 1H indicator walk and the 5M price-action walk share it unchanged.

No lookahead: on each bar the exit is checked against the stop *in force at the
start of that bar*, and only then is the breakeven move applied — so the bar that
first reaches +1R can still be stopped out at the original stop (you cannot know,
from OHLC alone, whether the high or the low came first). This matches the
backtest's existing conservative "if a bar could touch both stop and target,
assume the stop hit first" assumption.
"""


class ManagedExit:
    """
    Tracks one open position and decides, bar by bar, when it exits.

    Construct from the plan's direction/entry/stop/target. `model="fixed"`
    reproduces set-and-forget behaviour exactly (the stop never moves).
    `model="breakeven"` slides the stop to `entry ± be_buffer_r*risk` once the
    trade's favourable excursion reaches `be_at_r * risk`.

    `current_stop` and `moved_to_be` are exposed (read-only by convention) so a
    live position-manager can render "stop now at $X / moved to breakeven"
    without re-deriving anything.
    """

    def __init__(self, direction, entry, stop, target, *,
                 model="fixed", be_at_r=1.0, be_buffer_r=0.0,
                 trail_at_r=1.0, trail_distance_r=1.0, honor_target=None):
        if direction not in ("long", "short"):
            raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
        self.direction = direction
        self.entry = entry
        self.target = target
        self.risk = abs(entry - stop)
        self.model = model
        self.be_at_r = be_at_r
        self.be_buffer_r = be_buffer_r
        self.trail_at_r = trail_at_r
        self.trail_distance_r = trail_distance_r
        # A trailing stop is meant to let winners run *past* the fixed target, so
        # by default the target no longer caps a trailing trade — the trail is
        # the only profit-taking exit. fixed/breakeven still honour the target.
        # Callers can force either behaviour explicitly.
        self.honor_target = (model != "trailing") if honor_target is None else honor_target

        self.current_stop = stop
        self.moved_to_be = False
        self.extreme = entry  # most favourable price seen so far (for trailing)

    def on_bar(self, high, low, close):
        """
        Process one completed bar. Returns (outcome, exit_price):

          * ("loss", stop)        — a stop below entry was hit,
          * ("breakeven", stop)   — a stop sitting at entry was hit (~0R),
          * ("win", price)        — the target was hit, or a stop that had been
                                    trailed above entry locked in a profit,
          * (None, None)          — still open; the caller keeps feeding bars
                                    (and applies its own hold timeout).

        The outcome of a stop hit is classified purely by where the stop sits
        relative to entry, so the same logic serves fixed, breakeven and
        trailing exits. The stop is checked before the target (worse outcome
        wins a same-bar tie), and both are checked before the stop is advanced,
        so any breakeven/trail move only ever protects *subsequent* bars — the
        bar that first reaches the trigger can still hit the old stop (no
        intrabar lookahead).
        """
        if self.direction == "long":
            hit_stop = low <= self.current_stop
            hit_target = high >= self.target
        else:
            hit_stop = high >= self.current_stop
            hit_target = low <= self.target

        if hit_stop:
            return self._stop_outcome(), self.current_stop
        if self.honor_target and hit_target:
            return "win", self.target

        self._advance_stop(high, low)
        return None, None

    def _stop_outcome(self):
        """Label a stop hit by the stop's R relative to entry (uniform across
        models): above entry = a locked-in win, at entry = breakeven, below = loss."""
        gross_r = ((self.current_stop - self.entry) / self.risk if self.direction == "long"
                   else (self.entry - self.current_stop) / self.risk)
        if gross_r > 1e-9:
            return "win"
        if gross_r < -1e-9:
            return "loss"
        return "breakeven"

    def _advance_stop(self, high, low):
        if self.model == "breakeven":
            self._maybe_move_to_breakeven(high, low)
        elif self.model == "trailing":
            self._maybe_trail(high, low)

    def _maybe_move_to_breakeven(self, high, low):
        if self.moved_to_be:
            return
        favourable = (high - self.entry) if self.direction == "long" else (self.entry - low)
        if favourable >= self.be_at_r * self.risk:
            buffer = self.be_buffer_r * self.risk
            self.current_stop = (self.entry + buffer if self.direction == "long"
                                 else self.entry - buffer)
            self.moved_to_be = True

    def _maybe_trail(self, high, low):
        # Track the most favourable excursion, then — once it clears the
        # activation threshold — trail the stop trail_distance_r behind it,
        # never loosening it.
        if self.direction == "long":
            self.extreme = max(self.extreme, high)
            if self.extreme - self.entry >= self.trail_at_r * self.risk:
                candidate = self.extreme - self.trail_distance_r * self.risk
                if candidate > self.current_stop:
                    self.current_stop = candidate
                    self.moved_to_be = True
        else:
            self.extreme = min(self.extreme, low)
            if self.entry - self.extreme >= self.trail_at_r * self.risk:
                candidate = self.extreme + self.trail_distance_r * self.risk
                if candidate < self.current_stop:
                    self.current_stop = candidate
                    self.moved_to_be = True
