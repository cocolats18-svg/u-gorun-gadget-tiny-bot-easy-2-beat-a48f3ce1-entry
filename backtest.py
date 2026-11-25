"""Local replay driver for the Sundial Drift bot (dev tooling only).

Feeds the kit's dev dataset (136 hourly bars x 7 symbols) to a bot speaking
agent-fun/bars-replay/v1, implementing the market model the kit documents in
contracts/bars-protocol.ts:

  * orders decided on batch k fill at the open of bar k+1, against that
    bar's o_depth, with 10 bps fee, 5 bps modelled slippage and exact
    constant-product price impact (Fraction math, truncating divisions);
  * decision-time checks in reply order (max orders, duplicate client id,
    universe, zero amount, no bar, min notional, cash, open positions,
    weight cap, position size), then fill-time max-slippage / zero-qty;
  * the last batch's orders are rejected (after_end); positions flatten at
    the end at the latest close against c_depth.

Runs the bot twice, reports net PnL vs the buy-and-hold baseline (same
driver, a tiny inline baseline bot), trade count, max drawdown, and the
sha256 of each run's action log. Usage:

    python3 backtest.py --kit <kit dir> --bot <entry dir>
"""

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import sys
from fractions import Fraction

BPS = 10000
QUOTE_SCALE = 6
CAPITAL_UNITS = 10_000_000_000  # 10,000 USDC
FEE_BPS = 10
SLIPPAGE_BPS = 5
MIN_NOTIONAL = 1_000_000  # 1 USDC
MAX_OPEN_POSITIONS = 10
MAX_ORDERS_PER_BAR = 16
MAX_WEIGHT_BPS = 2500
INTERVAL_MS = 3_600_000
SYMBOL_ORDER = ["FARTCOIN", "JITOSOL", "POPCAT", "RAY", "SOL", "WETH", "WIF"]


def load_dataset(kit_dir):
    pools = json.load(open(os.path.join(kit_dir, "dataset", "pools.json")))
    scales = {}
    for pool in pools["pools"]:
        scales[pool["asset"]] = {
            "mint": pool["mint"],
            "price_scale": pool["price_scale"],
            "qty_scale": pool["decimals"],
        }
    bars = {}
    for symbol in SYMBOL_ORDER:
        rows = []
        with gzip.open(os.path.join(kit_dir, "dataset", "bars", symbol + ".ndjson.gz"), "rt") as f:
            for line in f:
                row = json.loads(line)
                row["symbol"] = symbol
                rows.append(row)
        rows.sort(key=lambda r: r["t"])
        bars[symbol] = rows
    return scales, bars


def value(qty, price, qty_scale, price_scale):
    return qty * price * 10 ** QUOTE_SCALE // 10 ** (qty_scale + price_scale)


class Pool:
    """One symbol's constant-product pool at one open, exact fractions."""

    def __init__(self, depths):
        # D = 1 / (1/D1 + 1/D2); D = D1 with a single hop.
        if len(depths) == 1:
            self.d = Fraction(depths[0])
        else:
            inv = sum(Fraction(1, d) for d in depths)
            self.d = Fraction(1, 1) / inv
        self.y = self.d

    def buy_factor(self, amount_raw):
        # M = y (y + a) / D^2, y becomes y + a
        m = self.y * (self.y + amount_raw) / (self.d * self.d)
        self.y = self.y + amount_raw
        return m

    def sell_factor(self, amount_raw):
        # M = (D^2 + a y) / y^2, y becomes D^2 y / (D^2 + a y)
        m = (self.d * self.d + amount_raw * self.y) / (self.y * self.y)
        self.y = self.d * self.d * self.y / (self.d * self.d + amount_raw * self.y)
        return m


class Replay:
    def __init__(self, scales, bars):
        self.scales = scales
        self.bars = bars
        self.n_bars = len(bars[SYMBOL_ORDER[0]])
        self.first_t = bars[SYMBOL_ORDER[0]][0]["t"]
        self.cash = CAPITAL_UNITS
        self.positions = {}  # symbol -> [qty, cost_quote_units]
        self.equity_peak = CAPITAL_UNITS
        self.max_drawdown = 0.0
        self.trades = 0
        self.equity_series = []
        self._next_cid = 1

    def equity(self, latest):
        total = self.cash
        for symbol, (qty, _cost) in self.positions.items():
            bar = latest.get(symbol)
            if bar is not None:
                s = self.scales[symbol]
                total += value(qty, int(bar["c"]), s["qty_scale"], s["price_scale"])
        return total

    def track_equity(self, latest):
        eq = self.equity(latest)
        self.equity_series.append(eq)
        if eq > self.equity_peak:
            self.equity_peak = eq
        dd = (self.equity_peak - eq) / self.equity_peak if self.equity_peak else 0.0
        if dd > self.max_drawdown:
            self.max_drawdown = dd
        return eq

    def hello(self):
        return {
            "type": "hello",
            "protocol": "agent-fun/bars-replay/v1",
            "seed": "20261125",
            "symbols": [
                {
                    "symbol": s,
                    "mint": self.scales[s]["mint"],
                    "price_scale": self.scales[s]["price_scale"],
                    "qty_scale": self.scales[s]["qty_scale"],
                }
                for s in SYMBOL_ORDER
            ],
            "capital": {"quote": "USDC", "quote_scale": QUOTE_SCALE, "quote_units": str(CAPITAL_UNITS)},
            "caps": {
                "max_open_positions": MAX_OPEN_POSITIONS,
                "max_orders_per_bar": MAX_ORDERS_PER_BAR,
                "max_weight_bps": MAX_WEIGHT_BPS,
                "shorts": False,
                "leverage": False,
            },
            "fees": {
                "fee_bps": FEE_BPS,
                "slippage_bps": SLIPPAGE_BPS,
                "min_notional_quote_units": str(MIN_NOTIONAL),
            },
        }

    def account_msg(self, latest):
        return {
            "cash_quote_units": str(self.cash),
            "positions": [
                {"symbol": s, "qty": str(q), "cost_quote_units": str(c)}
                for s, (q, c) in sorted(self.positions.items())
            ],
            "equity_quote_units": str(self.equity(latest)),
        }

    def _check_orders(self, orders, latest, is_last):
        """Decision-time checks, in reply order. Returns accepted orders."""
        accepted = []
        used_ids = set()
        accepted_buys_cash = 0
        accepted_buys = {}
        accepted_sells = {}
        for order in orders[:MAX_ORDERS_PER_BAR]:
            cid = order.get("client_id")
            if cid is None:
                # The shim fills in a fresh id when the bot leaves it out.
                cid = f"o{self._next_cid}"
                self._next_cid += 1
                order = dict(order, client_id=cid)
            symbol = order.get("symbol")
            side = order.get("side")
            if cid in used_ids:
                continue
            used_ids.add(cid)
            if symbol not in self.scales or side not in ("buy", "sell"):
                continue
            if is_last:
                continue
            bar = latest.get(symbol)
            if bar is None:
                continue
            s = self.scales[symbol]
            if side == "buy":
                notional = int(order.get("notional_quote_units", 0))
                if notional == 0 or notional < MIN_NOTIONAL:
                    continue
                if accepted_buys_cash + notional > self.cash:
                    continue
                held_qty = self.positions.get(symbol, (0, 0))[0]
                held_value = value(held_qty, int(bar["c"]), s["qty_scale"], s["price_scale"])
                new_symbols = {sym for sym in self.positions} | set(accepted_buys)
                if symbol not in new_symbols and len(new_symbols) >= MAX_OPEN_POSITIONS:
                    continue
                if (held_value + accepted_buys.get(symbol, 0) + notional) * BPS > MAX_WEIGHT_BPS * self.equity(latest):
                    continue
                accepted_buys_cash += notional
                accepted_buys[symbol] = accepted_buys.get(symbol, 0) + notional
                accepted.append(order)
            else:
                qty = int(order.get("qty", 0))
                if qty == 0:
                    continue
                held_qty = self.positions.get(symbol, (0, 0))[0]
                if qty > held_qty - accepted_sells.get(symbol, 0):
                    continue
                rest = held_qty - accepted_sells.get(symbol, 0) - qty
                if rest > 0 and value(qty, int(bar["c"]), s["qty_scale"], s["price_scale"]) < MIN_NOTIONAL:
                    continue
                accepted_sells[symbol] = accepted_sells.get(symbol, 0) + qty
                accepted.append(order)
        return accepted

    def _fill_orders(self, accepted, bars_next, latest):
        """Fill accepted orders at the next bars' opens. Returns fill dicts."""
        fills = []
        pools = {}
        for order in accepted:
            symbol = order["symbol"]
            bar = bars_next.get(symbol)
            if bar is None:
                fills.append({"client_id": order.get("client_id"), "symbol": symbol, "status": "rejected", "reason": "no_bar"})
                continue
            s = self.scales[symbol]
            pool = pools.setdefault(symbol, Pool([int(d) for d in bar["o_depth"]]))
            open_price = int(bar["o"])
            latest_close = int(latest[symbol]["c"])
            if order["side"] == "buy":
                notional = int(order["notional_quote_units"])
                spend = notional * (BPS - FEE_BPS) // BPS
                amount_raw = spend * 10 ** 6 // 10 ** QUOTE_SCALE
                m = pool.buy_factor(amount_raw)
                price = int(Fraction(open_price * (BPS + SLIPPAGE_BPS), BPS) * m)
                if price * BPS > latest_close * (BPS + int(order["max_slippage_bps"])):
                    fills.append({"client_id": order.get("client_id"), "symbol": symbol, "status": "rejected", "reason": "max_slippage"})
                    continue
                qty = spend * 10 ** (s["qty_scale"] + s["price_scale"]) // (price * 10 ** QUOTE_SCALE)
                if qty == 0:
                    fills.append({"client_id": order.get("client_id"), "symbol": symbol, "status": "rejected", "reason": "zero_amount"})
                    continue
                self.cash -= notional
                qty_old, cost_old = self.positions.get(symbol, (0, 0))
                self.positions[symbol] = [qty_old + qty, cost_old + notional]
                self.trades += 1
                fills.append({"client_id": order.get("client_id"), "symbol": symbol, "status": "filled", "price": str(price), "qty": str(qty), "notional_quote_units": str(notional)})
            else:
                qty = int(order["qty"])
                qty_old, cost_old = self.positions[symbol]
                a = value(qty, open_price, s["qty_scale"], s["price_scale"]) * 10 ** 6 // 10 ** QUOTE_SCALE
                m = pool.sell_factor(a)
                price = int(Fraction(open_price * (BPS - SLIPPAGE_BPS), BPS) / m)
                if price < 1:
                    price = 1
                if price * BPS < latest_close * (BPS - int(order["max_slippage_bps"])):
                    fills.append({"client_id": order.get("client_id"), "symbol": symbol, "status": "rejected", "reason": "max_slippage"})
                    continue
                gross = value(qty, price, s["qty_scale"], s["price_scale"])
                proceeds = gross * (BPS - FEE_BPS) // BPS
                self.cash += proceeds
                left = qty_old - qty
                new_cost = cost_old * left // qty_old if qty_old else 0
                if left == 0:
                    del self.positions[symbol]
                else:
                    self.positions[symbol] = [left, new_cost]
                self.trades += 1
                fills.append({"client_id": order.get("client_id"), "symbol": symbol, "status": "filled", "price": str(price), "qty": str(qty), "notional_quote_units": str(proceeds)})
        return fills

    def flatten(self, latest):
        """End-of-replay flatten at latest closes against c_depth."""
        for symbol in sorted(self.positions):
            qty, _cost = self.positions[symbol]
            bar = latest[symbol]
            s = self.scales[symbol]
            pool = Pool([int(d) for d in bar["c_depth"]])
            close_price = int(bar["c"])
            a = value(qty, close_price, s["qty_scale"], s["price_scale"]) * 10 ** 6 // 10 ** QUOTE_SCALE
            m = pool.sell_factor(a)
            price = int(Fraction(close_price * (BPS - SLIPPAGE_BPS), BPS) / m)
            if price < 1:
                price = 1
            gross = value(qty, price, s["qty_scale"], s["price_scale"])
            self.cash += gross * (BPS - FEE_BPS) // BPS
            self.trades += 1
        self.positions = {}

    def run_bot(self, bot_dir):
        """Run the bot as a subprocess over the whole dev window. Returns (log_bytes, fills)."""
        proc = subprocess.Popen(
            [sys.executable, "-P", "-s", os.path.join(bot_dir, "main.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        log = []
        latest = {}
        pending = []
        proc.stdin.write(json.dumps(self.hello()) + "\n")
        proc.stdin.flush()
        for k in range(self.n_bars):
            t = self.first_t + k * INTERVAL_MS
            bars_now = {}
            for symbol in SYMBOL_ORDER:
                row = self.bars[symbol][k]
                if row["t"] == t:
                    bars_now[symbol] = row
            # Resolve last batch's orders at this batch's opens.
            fills = self._fill_orders(pending, bars_now, latest) if pending else []
            latest.update(bars_now)
            self.track_equity(latest)
            batch = {
                "type": "batch",
                "seq": k,
                "t": t,
                "bars": [bars_now[s] for s in SYMBOL_ORDER if s in bars_now],
                "fills": fills,
                "account": self.account_msg(latest),
            }
            proc.stdin.write(json.dumps(batch) + "\n")
            proc.stdin.flush()
            reply = proc.stdout.readline()
            if not reply:
                raise RuntimeError(f"bot exited early at batch {k}: {proc.stderr.read()[:500]}")
            log.append(reply)
            msg = json.loads(reply)
            assert msg.get("type") == "reply" and msg.get("seq") == k, f"bad reply at batch {k}: {reply[:200]}"
            pending = self._check_orders(msg.get("orders", []), latest, is_last=(k == self.n_bars - 1))
        # The last batch's orders are rejected (after_end); flatten and finish.
        self.flatten(latest)
        final_equity = self.cash
        proc.stdin.write(json.dumps({"type": "done", "seq": self.n_bars, "account": self.account_msg(latest)}) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"bot exited {proc.returncode}: {proc.stderr.read()[:1000]}")
        return "".join(log).encode(), final_equity


def run_baseline_hold(scales, bars):
    """Buy-and-hold through the same market model, for comparison."""
    replay = Replay(scales, bars)
    latest = {}
    pending = []
    decided = False
    for k in range(replay.n_bars):
        t = replay.first_t + k * INTERVAL_MS
        bars_now = {}
        for symbol in SYMBOL_ORDER:
            row = bars[symbol][k]
            if row["t"] == t:
                bars_now[symbol] = row
        fills = replay._fill_orders(pending, bars_now, latest) if pending else []
        latest.update(bars_now)
        replay.track_equity(latest)
        orders = []
        if not decided and latest:
            priced = [s for s in SYMBOL_ORDER if s in latest]
            n = min(len(priced), MAX_OPEN_POSITIONS, MAX_ORDERS_PER_BAR)
            even = replay.cash // n
            by_weight = MAX_WEIGHT_BPS * replay.equity(latest) // BPS
            sl = even if even < by_weight else by_weight
            if sl >= MIN_NOTIONAL:
                orders = [{"side": "buy", "symbol": s, "notional_quote_units": sl, "max_slippage_bps": 10000} for s in priced[:n]]
                decided = True
        pending = replay._check_orders(orders, latest, is_last=(k == replay.n_bars - 1))
    replay.flatten(latest)
    return replay.cash, replay


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True)
    ap.add_argument("--bot", required=True)
    args = ap.parse_args()

    scales, bars = load_dataset(args.kit)
    results = []
    for run in (1, 2):
        replay = Replay(scales, bars)
        log, final_equity = replay.run_bot(args.bot)
        digest = hashlib.sha256(log).hexdigest()
        pnl_usdc = (final_equity - CAPITAL_UNITS) / 1_000_000
        results.append((digest, pnl_usdc, replay.trades, replay.max_drawdown))
        print(f"run {run}: net PnL {pnl_usdc:+.2f} USDC | trades {replay.trades} | "
              f"max drawdown {replay.max_drawdown:.2%} | action-log sha256 {digest}")

    bah_equity, bah = run_baseline_hold(scales, bars)
    bah_pnl = (bah_equity - CAPITAL_UNITS) / 1_000_000
    print(f"buy-and-hold baseline: net PnL {bah_pnl:+.2f} USDC | trades {bah.trades} | "
          f"max drawdown {bah.max_drawdown:.2%}")
    bot_pnl = results[0][1]
    print(f"margin over buy-and-hold: {bot_pnl - bah_pnl:+.2f} USDC")
    print(f"runs byte-identical: {results[0][0] == results[1][0]}")
    print(f"guardrails: min trades 8 -> {'PASS' if results[0][2] >= 8 else 'FAIL'} "
          f"({results[0][2]} trades); max drawdown 25% -> "
          f"{'PASS' if results[0][3] <= 0.25 else 'FAIL'} ({results[0][3]:.2%})")


if __name__ == "__main__":
    main()
