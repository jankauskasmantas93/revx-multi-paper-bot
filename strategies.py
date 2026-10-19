"""
The two strategies being compared. Each one looks ONLY at candles that have
already closed (all indicators use the current and past bars), and the
engine fills its orders at the NEXT bar's open. That avoids look-ahead bias.

Every strategy provides:
  prepare(md)             -> pre-computes indicators (arrays: bars x coins)
  decide(t, held, can_buy) -> (coins to sell, coins to buy in priority order)
  atr                     -> ATR array used by the engine's trailing stop
  stop_atr                -> trailing stop distance in ATRs
  max_positions
"""
import numpy as np
import pandas as pd


def _atr(md, n):
    prev = md["close"].shift(1)
    tr = pd.concat([md["high"] - md["low"], (md["high"] - prev).abs(),
                    (md["low"] - prev).abs()]).groupby(level=0).max()
    tr = tr.reindex(md["close"].index)
    return tr.rolling(n, min_periods=n).mean()


def common_filters(md, cfg):
    """Returns (liquid[t,i], regime_on[t]) — both known at the close of bar t."""
    qv = (md["volume"] * md["close"]).rolling(24, min_periods=24).sum()
    liquid = (qv >= cfg["min_volume"]).to_numpy()
    spread_ok = np.array([cfg["half_spread"].get(c, cfg["default_half_spread"]) * 2
                          <= cfg["max_spread"] for c in md["close"].columns])
    liquid = liquid & spread_ok[None, :]
    n = cfg["regime_hours"]
    if "BTC" in md["close"].columns and n:
        btc = md["close"]["BTC"]
        sma = btc.rolling(n, min_periods=n).mean()
        regime = (btc > sma).to_numpy()
    else:
        regime = np.ones(len(md["close"]), bool)
    if cfg.get("breadth_min") and n:
        # market breadth: share of coins trading above their own long average
        c = md["close"]
        sma_all = c.rolling(n, min_periods=n).mean()
        above = (c > sma_all).sum(axis=1)
        counted = sma_all.notna().sum(axis=1)
        breadth = (above / counted.replace(0, np.nan)).fillna(0).to_numpy()
        regime = regime & (breadth >= cfg["breadth_min"])
    return liquid, regime


class Breakout:
    """Buy a coin when it closes above its highest high of the last `lookback`
    hours (market uptrend only). Exit on the ATR trailing stop, or when it closes
    below its lowest low of `exit_lookback` hours. Strongest movers are bought first."""
    name = "breakout"

    def __init__(self, lookback=48, stop_atr=3.0, max_positions=4,
                 exit_lookback=None, atr_len=24, max_hold=None):
        self.lookback, self.stop_atr, self.max_positions = lookback, stop_atr, max_positions
        self.exit_lookback = exit_lookback or max(lookback // 2, 6)
        self.atr_len, self.max_hold = atr_len, max_hold

    def prepare(self, md, cfg):
        c = md["close"]
        hh = md["high"].rolling(self.lookback, min_periods=self.lookback).max().shift(1)
        ll = md["low"].rolling(self.exit_lookback, min_periods=self.exit_lookback).min().shift(1)
        self.liquid, self.regime = common_filters(md, cfg)
        self.entry = ((c > hh).to_numpy() & self.liquid)
        self.exit = (c < ll).to_numpy()
        self.score = (c / c.shift(self.lookback) - 1).to_numpy()
        self.atr = _atr(md, self.atr_len).to_numpy()

    def decide(self, t, held, can_buy):
        sells = [i for i in held if self.exit[t, i]
                 or (self.max_hold and t - held[i]["entry_t"] >= self.max_hold)]
        buys = []
        if can_buy and self.regime[t]:
            cand = np.where(self.entry[t])[0]
            cand = [i for i in cand if i not in held and np.isfinite(self.score[t, i])]
            buys = sorted(cand, key=lambda i: -self.score[t, i])
        return sells, buys


class Rotation:
    """Every `rebalance_hours`, rank coins by risk-adjusted momentum over
    `lookback` hours and hold the top `max_positions` (only those with positive
    momentum, and only while BTC is in an uptrend). A held coin is kept while it
    stays within the top max_positions + buffer, which cuts pointless trading."""
    name = "rotation"

    def __init__(self, lookback=168, rebalance_hours=24, max_positions=4,
                 buffer=2, stop_atr=5.0, atr_len=24):
        self.lookback, self.rebalance_hours = lookback, rebalance_hours
        self.max_positions, self.buffer = max_positions, buffer
        self.stop_atr, self.atr_len = stop_atr, atr_len

    def prepare(self, md, cfg):
        c = md["close"]
        ret = c.pct_change(fill_method=None)
        mom = c / c.shift(self.lookback) - 1
        vol = ret.rolling(self.lookback, min_periods=self.lookback // 2).std() * np.sqrt(self.lookback)
        self.score = (mom / vol).to_numpy()
        self.mom = mom.to_numpy()
        self.liquid, self.regime = common_filters(md, cfg)
        hours = np.asarray((c.index - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1))
        self.rebal = (hours % self.rebalance_hours) == (self.rebalance_hours - 1)
        self.atr = _atr(md, self.atr_len).to_numpy()

    def decide(self, t, held, can_buy):
        if not self.rebal[t]:
            return [], []
        if not self.regime[t]:
            return list(held), []
        s = self.score[t]
        ok = np.isfinite(s) & self.liquid[t] & (self.mom[t] > 0)
        ranked = [i for i in np.argsort(-np.where(ok, s, -np.inf)) if ok[i]]
        keep = set(ranked[: self.max_positions + self.buffer])
        sells = [i for i in held if i not in keep]
        slots = self.max_positions - (len(held) - len(sells))
        buys = [i for i in ranked[: self.max_positions] if i not in held][:max(slots, 0)] if can_buy else []
        return sells, buys


STRATEGIES = {"breakout": Breakout, "rotation": Rotation}
