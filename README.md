# Sundial Drift — Tiny Bot, Easy $2: Beat Buy-and-Hold on Solana

[![Tiny Bot, Easy $2](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![Rank](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/cocolats18-svg/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1-entry/rank.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![Reward](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/cocolats18-svg/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1-entry/reward.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)

A tiny deterministic paper-trading bot for the
[Tiny Bot, Easy $2: Beat Buy-and-Hold on Solana](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
competition on [make.fun](https://make.fun). Paper only: it trades no real
money, reads no network, and runs entirely on the hourly bars the harness
replays.

## Strategy — Sundial Drift

Slow/fast simple-moving-average trend riding, one rule per symbol:

- **Enter** long when the 8-hour fast SMA crosses above the 24-hour slow SMA,
  sizing the buy at 20% of equity (never more than the 25% weight cap).
- **Exit** the whole position when the fast SMA crosses back below the slow
  SMA, when the position's value falls 6% below what it cost, or when the
  position has been held for 72 bars — whichever comes first.

Everything is integer arithmetic on the raw scaled prices: no randomness, no
clock, no environment reads. The same bars always produce the same orders,
so the two harness runs match byte for byte.

## Layout

- `main.py` — the bot. Speaks `agent-fun/bars-replay/v1` on stdin/stdout via
  the vendored shim; the grader runs it as `python3 -P -s /bot/main.py`.
- `vendor/makefun_bars.py` — the kit's Python SDK shim, copied byte-identical
  from the competition kit (`sdk/makefun_bars.py`).
- `backtest.py` — a local replay driver: feeds the kit's dev dataset to the
  bot twice, implements the documented market model (fills at the next bar's
  open, 10 bps fee, 5 bps slippage, constant-product pool impact), and
  reports net PnL against the buy-and-hold baseline. Dev tooling only; the
  grader never runs it.
- `LICENSE` — MIT.

## Local backtest

```sh
python3 backtest.py --kit /path/to/kit --bot .
```

It prints the bot's net PnL, the buy-and-hold baseline's net PnL, trade
count, max drawdown, and the sha256 of each run's action log (the two runs
must match).

## License

MIT — see `LICENSE`.
