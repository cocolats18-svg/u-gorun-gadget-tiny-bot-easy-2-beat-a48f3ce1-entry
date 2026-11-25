"""Sundial Drift — a tiny deterministic paper bot for hourly Solana bars.

Strategy (fresh, original): slow/fast simple-moving-average trend riding.
For each symbol it keeps the closes it has seen, computes an 8-hour fast SMA
and a 24-hour slow SMA, and:

  * enters long when the fast SMA crosses above the slow SMA (trend turns up)
    and the latest close is still above the fast SMA — a crossover whose
    close has already slipped back under it is a fading move, not a trend
    worth chasing — sizing the buy at 20% of equity (never more than the
    25% weight cap);
  * exits the whole position when the fast SMA crosses back below the slow
    SMA, when the position's value falls 6% below what it cost, or when the
    position has been held for 72 bars — whichever comes first.

Everything is integer arithmetic on the raw scaled prices, no randomness, no
clock, no environment, no network: the same bars always produce the same
orders. Paper only — the harness replays historical bars and fills orders at
the next bar's open.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

FAST_BARS = 8          # fast SMA window, in hourly bars
SLOW_BARS = 24         # slow SMA window, in hourly bars
MAX_HOLD_BARS = 72     # time exit: close a position after this many bars
STOP_BPS = 600         # exit when value falls 6% below the position's cost
SIZE_BPS = 2000        # target 20% of equity per entry
MAX_SLIPPAGE_BPS = 200  # 2% — wide enough for the 5 bps modelled slippage
                        # plus pool impact, tight enough to stay sane

_history = {}      # symbol -> list of closes (ints, oldest first)
_above = {}        # symbol -> whether fast SMA was above slow SMA last batch
_opened_seq = {}   # symbol -> seq of the batch that opened the current position


def _sma(closes, window):
    """Mean of the last `window` closes, truncated to an int."""
    return sum(closes[-window:]) // window


def on_batch(state, batch):
    # Warm-up batches are for building history only: every order decided on
    # one is rejected, so decide nothing until the scored window starts.
    if state.warming_up:
        return []

    orders = []
    min_notional = int(state.hello["fees"]["min_notional_quote_units"])

    for entry in state.hello["symbols"]:
        symbol = entry["symbol"]
        close = state.close(symbol)
        if close is None:
            continue

        closes = _history.setdefault(symbol, [])
        closes.append(close)
        if len(closes) < SLOW_BARS:
            continue

        fast = _sma(closes, FAST_BARS)
        slow = _sma(closes, SLOW_BARS)
        is_above = fast > slow
        was_above = _above.get(symbol)
        _above[symbol] = is_above

        held = state.positions.get(symbol)
        if held is not None:
            # Exit: trend flipped down, stop-loss hit, or held too long.
            value = state.value(symbol, held.qty)
            stop_hit = value * 10000 < held.cost * (10000 - STOP_BPS)
            aged_out = state.seq - _opened_seq.get(symbol, state.seq) >= MAX_HOLD_BARS
            if (was_above is True and not is_above) or stop_hit or aged_out:
                orders.append(
                    {
                        "side": "sell",
                        "symbol": symbol,
                        "qty": held.qty,
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    }
                )
            continue

        # No position: forget the open seq, and enter on an upward crossover
        # only while the price itself is still above the fast average — a
        # crossover whose close has already slipped back under the fast SMA
        # is a fading move, not a trend worth chasing.
        _opened_seq.pop(symbol, None)
        if was_above is False and is_above and close > fast:
            room = state.weight_room(symbol)
            target = state.equity * SIZE_BPS // 10000
            notional = min(state.cash // 4, target, room)
            if notional >= min_notional:
                orders.append(
                    {
                        "side": "buy",
                        "symbol": symbol,
                        "notional_quote_units": notional,
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    }
                )
                _opened_seq[symbol] = state.seq

    return orders


makefun_bars.run(on_batch)
