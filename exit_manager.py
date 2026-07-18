"""
Exit management — the shared, pure decision half of "how a filled trade is
managed", mirroring how evaluate_plan is the shared decision half of "what
trade to take".

Today the only model is "fixed" (set-and-forget: the trade runs to its original
stop, its target, or the caller's hold timeout) plus "breakeven" (once price has
gone far enough in favour, slide the stop up to entry so the trade can no longer
lose). The class is deliberately a *stepper* fed one bar at a time and holding no
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
                 model="fixed", be_at_r=1.0, be_buffer_r=0.0):
        if direction not in ("long", "short"):
            raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
        self.direction = direction
        self.entry = entry
        self.target = target
        self.risk = abs(entry - stop)
        self.model = model
        self.be_at_r = be_at_r
        self.be_buffer_r = be_buffer_r

        self.current_stop = stop
        self.moved_to_be = False

    def on_bar(self, high, low, close):
        """
        Process one completed bar. Returns (outcome, exit_price):

          * ("loss", stop)        — the (original) stop was hit,
          * ("breakeven", stop)   — the stop was hit after being moved to entry,
          * ("win", target)       — the target was hit,
          * (None, None)          — still open; the caller keeps feeding bars
                                    (and applies its own hold timeout).

        The stop is checked before the target (worse outcome wins a same-bar
        tie), and both are checked before any breakeven move is applied, so the
        move only ever protects *subsequent* bars.
        """
        if self.direction == "long":
            hit_stop = low <= self.current_stop
            hit_target = high >= self.target
        else:
            hit_stop = high >= self.current_stop
            hit_target = low <= self.target

        if hit_stop:
            outcome = "breakeven" if self.moved_to_be else "loss"
            return outcome, self.current_stop
        if hit_target:
            return "win", self.target

        self._maybe_move_to_breakeven(high, low)
        return None, None

    def _maybe_move_to_breakeven(self, high, low):
        if self.model == "fixed" or self.moved_to_be:
            return
        if self.direction == "long":
            favourable = high - self.entry
        else:
            favourable = self.entry - low
        if favourable >= self.be_at_r * self.risk:
            buffer = self.be_buffer_r * self.risk
            self.current_stop = (self.entry + buffer if self.direction == "long"
                                 else self.entry - buffer)
            self.moved_to_be = True
