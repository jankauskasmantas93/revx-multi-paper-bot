"""
The trading book: cash, positions, stops, sizing and the order rules.

The backtester (backtest.py) and the paper/live bot (paper_trader.py) both use
this same code, so the bot makes exactly the decisions the backtest tested.

Coins are referred to by their column number i in the price table.
Prices passed in are arrays with one value per coin (NaN = no price).
"""
import numpy as np
import pandas as pd


class Book:
    def __init__(self, coins, cfg, stop_atr, max_positions):
        self.coins, self.cfg = list(coins), cfg
        self.stop_atr, self.K = stop_atr, max_positions
        self.cash = cfg["capital"]
        self.pos, self.trades = {}, []
        self.pend_sell, self.pend_buy, self.pend_add = [], [], []
        self.equity_prev = self.cash
        self.day_start_eq, self.can_buy, self.cur_day = self.cash, True, None
        self.last_px = np.full(len(self.coins), np.nan)

    # ---------------------------------------------------------------- fills
    def _buy(self, i, raw, hs_i, notional):
        cfg = self.cfg
        px = raw * (1 + hs_i + cfg["slippage"])
        cost = notional * (1 + cfg["fee"])
        self.cash -= cost
        return notional / px, px, cost

    def _sell(self, i, raw, hs_i, when, reason):
        cfg = self.cfg
        p = self.pos.pop(i)
        px = raw * (1 - hs_i - cfg["slippage"])
        proceeds = p["qty"] * px * (1 - cfg["fee"])
        self.cash += proceeds
        self.trades.append({
            "coin": self.coins[i], "entry_time": p["entry_time"], "exit_time": when,
            "entry_price": p["entry_px"], "exit_price": px,
            "cost_usd": p["cost"], "pnl_usd": proceeds - p["cost"],
            "return_pct": (proceeds / p["cost"] - 1) * 100,
            "hours_held": int(round((when - p["entry_time"]) / pd.Timedelta(hours=1))),
            "adds": p["adds"], "exit_reason": reason})
        return self.trades[-1]

    def execute(self, t, when, raw, hs, atr_prev):
        """Carry out the orders planned at the last close.
        raw: fill price per coin (backtest: this bar's open; live: current mid price)
        hs:  half-spread per coin (live: 0 for sells at the bid is handled by caller)
        atr_prev: ATR per coin at the close the orders were planned on."""
        cfg, K, cap = self.cfg, self.K, self.cfg["position_cap"]
        fee = cfg["fee"]
        done = []
        carry = []
        for i in self.pend_sell:
            if i in self.pos:
                if np.isfinite(raw[i]):
                    done.append(("SELL", self._sell(i, raw[i], hs[i], when, "signal")))
                else:
                    carry.append(i)
        eq = self.equity_prev
        for i in self.pend_buy:
            if len(self.pos) >= K or i in self.pos or not np.isfinite(raw[i]):
                continue
            atr = atr_prev[i]
            if not np.isfinite(atr):
                continue
            if cfg["risk_per_trade"]:
                stop_frac = self.stop_atr * atr / raw[i]
                notional = eq * cfg["risk_per_trade"] / max(stop_frac, 1e-4)
            else:
                notional = eq / K
            notional = min(notional, eq * cap, self.cash / (1 + fee))
            if notional < cfg["min_order"]:
                continue
            qty, px, cost = self._buy(i, raw[i], hs[i], notional)
            self.pos[i] = {"qty": qty, "entry_t": t, "entry_time": when, "entry_px": px,
                           "cost": cost, "peak": raw[i], "stop": raw[i] - self.stop_atr * atr,
                           "first_open": raw[i], "first_notional": notional, "adds": 0,
                           "last_buy": raw[i]}
            done.append(("BUY", {"coin": self.coins[i], "usd": notional, "price": px, "qty": qty}))
        for i in self.pend_add:
            if i not in self.pos or not np.isfinite(raw[i]):
                continue
            p = self.pos[i]
            room = eq * cap - p["qty"] * raw[i]
            notional = min(p["first_notional"] * cfg["add_fraction"], room, self.cash / (1 + fee))
            if notional < cfg["min_order"]:
                continue
            qty, px, cost = self._buy(i, raw[i], hs[i], notional)
            p["entry_px"] = (p["entry_px"] * p["qty"] + px * qty) / (p["qty"] + qty)
            p["qty"] += qty
            p["cost"] += cost
            p["adds"] += 1
            p["last_buy"] = raw[i]
            done.append(("ADD", {"coin": self.coins[i], "usd": notional, "price": px, "qty": qty}))
        self.pend_sell, self.pend_buy, self.pend_add = carry, [], []
        return done

    def stops_from_candle(self, when, low, open_, hs):
        """Stop check against a finished candle's low (fill at the stop, or the open if it gapped)."""
        out = []
        for i in list(self.pos):
            p = self.pos[i]
            if np.isfinite(low[i]) and low[i] <= p["stop"]:
                out.append(self._sell(i, min(open_[i], p["stop"]), hs[i], when, "stop"))
        return out

    def stops_live(self, when, bid):
        """Stop check against the live bid price (sells at the bid)."""
        out = []
        for i in list(self.pos):
            if np.isfinite(bid[i]) and bid[i] <= self.pos[i]["stop"]:
                out.append(self._sell(i, bid[i], 0.0, when, "stop"))
        return out

    # ---------------------------------------------------------------- close
    def mark(self, close, atr_now, day):
        """At a candle close: update prices, ratchet stops up, update equity and the
        daily loss limit. Returns equity."""
        cfg = self.cfg
        fin = np.isfinite(close)
        self.last_px[fin] = close[fin]
        for i, p in self.pos.items():
            if np.isfinite(close[i]) and np.isfinite(atr_now[i]):
                p["peak"] = max(p["peak"], close[i])
                mult = self.stop_atr
                if cfg["lock_gain"] and p["peak"] / p["first_open"] - 1 >= cfg["lock_gain"]:
                    mult = min(mult, cfg["lock_atr"])
                p["stop"] = max(p["stop"], p["peak"] - mult * atr_now[i])
        equity = self.cash + sum(p["qty"] * self.last_px[i] for i, p in self.pos.items())
        self.equity_prev = equity
        if day != self.cur_day:
            self.cur_day, self.day_start_eq, self.can_buy = day, equity, True
        if equity < self.day_start_eq * (1 - cfg["daily_loss_limit"]):
            self.can_buy = False
        return equity

    def plan(self, t, strat, close, atr_now):
        """Decide at this close what to do at the next opportunity."""
        cfg = self.cfg
        sells, buys = strat.decide(t, self.pos, self.can_buy)
        self.pend_sell = list(set(self.pend_sell) | set(sells))
        self.pend_buy = [i for i in buys if i not in sells]
        if cfg["max_adds"] and self.can_buy and strat.regime[t]:
            self.pend_add = [i for i, p in self.pos.items()
                             if i not in self.pend_sell and p["adds"] < cfg["max_adds"]
                             and np.isfinite(close[i]) and np.isfinite(atr_now[i])
                             and close[i] >= p["last_buy"] + cfg["add_atr"] * atr_now[i]]

    # ---------------------------------------------------------------- saving
    def to_dict(self):
        name = lambda i: self.coins[i]
        pos = {name(i): {k: (v.isoformat() if isinstance(v, pd.Timestamp) else
                             (float(v) if isinstance(v, (np.floating, float)) else v))
                         for k, v in p.items() if k != "entry_t"}
               for i, p in self.pos.items()}
        return {"cash": float(self.cash), "positions": pos,
                "equity_prev": float(self.equity_prev), "day_start_eq": float(self.day_start_eq),
                "can_buy": bool(self.can_buy),
                "cur_day": None if self.cur_day is None else int(self.cur_day),
                "pend_sell": [name(i) for i in self.pend_sell],
                "pend_buy": [name(i) for i in self.pend_buy],
                "pend_add": [name(i) for i in self.pend_add]}

    def load_dict(self, d, index):
        """Restore from to_dict(). `index` is the current price-table index (for entry_t)."""
        col = {c: i for i, c in enumerate(self.coins)}
        self.cash = d["cash"]
        self.equity_prev, self.day_start_eq = d["equity_prev"], d["day_start_eq"]
        self.can_buy, self.cur_day = d["can_buy"], d["cur_day"]
        self.pos = {}
        for coin, p in d["positions"].items():
            if coin not in col:
                raise RuntimeError(f"Held coin {coin} is missing from the data folder")
            p = dict(p)
            p["entry_time"] = pd.Timestamp(p["entry_time"])
            p["entry_t"] = int(index.searchsorted(p["entry_time"]))
            self.pos[col[coin]] = p
        for attr in ["pend_sell", "pend_buy", "pend_add"]:
            setattr(self, attr, [col[c] for c in d.get(attr, []) if c in col])
