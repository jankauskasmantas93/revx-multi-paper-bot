"""
Tests for the paper trader (no internet needed):  python test_paper.py

1. Replay: feed old candles to the paper trader one hour at a time, restarting
   it from its saved file every hour, with live prices set to the next candle's
   open. It must produce EXACTLY the same equity and trades as the backtest.
2. Catch-up: after a long gap (laptop asleep) it applies stops to the missed
   candles and carries on.
3. Safety: stale data is not traded; bad prices (>5% jump) are skipped.
"""
import os
import shutil
import tempfile

import numpy as np
import pandas as pd

import backtest as B
import config as C
import paper_trader as P
import self_test as S
from strategies import STRATEGIES

HOUR = pd.Timedelta(hours=1)


class FakeHooks:
    """Pretends to be Revolut X: data up to `now`, live price = next candle's open."""

    def __init__(self, md, hs, jump_coin=None):
        self.md, self.hs, self.now, self.jump_coin = md, hs, None, jump_coin
        self.coins_ = list(md["close"].columns)

    def update_data(self, now):
        self.now = now

    def load_md(self):
        last = self.now.floor("h") - HOUR
        T = int(self.md["close"].index.searchsorted(last, side="right"))
        return {k: v.iloc[:T] for k, v in self.md.items()}

    def tickers(self, coins):
        idx = self.md["close"].index
        t = int(idx.searchsorted(self.now.floor("h")))
        out = {}
        for c in coins:
            o = self.md["open"][c].iloc[t] if t < len(idx) else np.nan
            if c == self.jump_coin:
                o *= 1.2
            h = self.hs.get(c, C.DEFAULT_HALF_SPREAD)
            out[c] = (o * (1 - h), o * (1 + h))          # bid/ask around the next open
        return out


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")
    return ok


def main():
    tmp = tempfile.mkdtemp()
    data = os.path.join(tmp, "data")
    S.make_demo_data(data)
    md = B.load_data(data, verbose=False)
    hs = B.load_half_spreads(data)
    idx = md["close"].index
    results = []
    params = {"lookback": 48, "stop_atr": 3.0, "max_positions": 3}

    for profile in ["standard", "aggressive", "slow_trend"]:
        p = dict(params, lookback=480, stop_atr=12.0) if profile == "slow_trend" else params
        t0, t1 = 2000, 2600 if profile != "slow_trend" else 3400
        pdir = os.path.join(tmp, f"paper_{profile}")
        hooks = FakeHooks(md, hs)
        for t in range(t0, t1):
            # a fresh trader every hour, loaded from its saved file (like a restart)
            tr = P.PaperTrader(paper_dir=pdir, data_dir=data, hooks=hooks, profile=profile,
                               strategy="breakout", params=p, capital=135.0)
            assert tr.cycle(idx[t] + HOUR + pd.Timedelta(seconds=90))
        cfg = B.profile_cfg(B.base_cfg(hs, capital=135.0), profile)
        eq_bt, tr_bt = B.run(md, STRATEGIES["breakout"](**p), cfg, t0, t1)
        eq_pp = pd.read_csv(os.path.join(pdir, "equity.csv"))
        tpath = os.path.join(pdir, "trades.csv")
        tr_pp = pd.read_csv(tpath) if os.path.exists(tpath) else pd.DataFrame()
        same_eq = len(eq_pp) == len(eq_bt) and np.allclose(eq_pp["equity"].to_numpy(),
                                                           eq_bt.to_numpy(), rtol=1e-9, atol=1e-6)
        same_tr = len(tr_pp) == len(tr_bt) and (len(tr_bt) == 0 or (
            list(tr_pp["coin"]) == list(tr_bt["coin"])
            and np.allclose(tr_pp["pnl_usd"], tr_bt["pnl_usd"], rtol=1e-9)
            and list(tr_pp["exit_reason"]) == list(tr_bt["exit_reason"])))
        results.append(check(f"[{profile}] paper trader == backtest, hour by hour with restarts",
                             same_eq and same_tr and len(tr_bt) > 0,
                             f"({len(tr_bt)} trades, final ${eq_bt.iloc[-1]:.2f} vs "
                             f"${eq_pp['equity'].iloc[-1]:.2f})"))

    # 2. catch-up after the laptop was asleep for 3 days
    pdir = os.path.join(tmp, "paper_gap")
    hooks = FakeHooks(md, hs)
    t0 = 2000
    for t in list(range(t0, t0 + 300)) + list(range(t0 + 372, t0 + 600)):
        tr = P.PaperTrader(paper_dir=pdir, data_dir=data, hooks=hooks, profile="standard",
                           strategy="breakout", params=params, capital=135.0)
        tr.cycle(idx[t] + HOUR + pd.Timedelta(seconds=90))
    eq_pp = pd.read_csv(os.path.join(pdir, "equity.csv"))
    results.append(check("catch-up: every missed hour is still recorded after a 3-day sleep",
                         len(eq_pp) == 600 and eq_pp["bar"].is_unique, f"({len(eq_pp)} of 600 hours)"))

    # 3a. stale data: the candle isn't there yet -> no trading, nothing saved
    pdir = os.path.join(tmp, "paper_stale")
    hooks = FakeHooks(md, hs)
    tr = P.PaperTrader(paper_dir=pdir, data_dir=data, hooks=hooks, profile="standard",
                       strategy="breakout", params=params, capital=135.0)
    tr.cycle(idx[2000] + HOUR + pd.Timedelta(seconds=90))
    hooks.load_md = lambda: {k: v.iloc[:2001] for k, v in md.items()}   # data stuck in the past
    ok = tr.cycle(idx[2005] + HOUR + pd.Timedelta(seconds=90))
    results.append(check("stale data: refuses to trade when the newest candle is missing", ok is False))

    # 3b. bad price guard: a coin whose live price is 20% off never gets bought
    pdir = os.path.join(tmp, "paper_jump")
    victim = "COIN3"
    hooks = FakeHooks(md, hs, jump_coin=victim)
    for t in range(2000, 2600):
        tr = P.PaperTrader(paper_dir=pdir, data_dir=data, hooks=hooks, profile="standard",
                           strategy="breakout", params=params, capital=135.0)
        tr.cycle(idx[t] + HOUR + pd.Timedelta(seconds=90))
    orders = pd.read_csv(os.path.join(pdir, "orders.csv"))
    bought = set(orders.loc[orders["action"] == "BUY", "coin"])
    results.append(check("bad price guard: coin with a 20% price jump is never bought",
                         victim not in bought and len(bought) > 0, f"(bought {sorted(bought)})"))

    # 4. the real internet code path, against a fake Revolut X API
    import revx_public as api
    live_dir = os.path.join(tmp, "live_data")
    os.makedirs(live_dir)
    H = 3000
    cut_ms = int(idx[H - 1].timestamp() * 1000)
    for f in os.listdir(data):
        d = pd.read_csv(os.path.join(data, f))
        if f != "spreads.csv":
            d = d[d["start"] <= cut_ms]
        d.to_csv(os.path.join(live_dir, f), index=False)
    os.utime(os.path.join(live_dir, "spreads.csv"), (0, 0))      # old -> gets refreshed
    raw = {os.path.splitext(f)[0]: pd.read_csv(os.path.join(data, f))
           for f in os.listdir(data) if f != "spreads.csv"}
    clock = {"now": None}

    def fake_candles(sym, interval, since, until, region):
        d = raw[sym.split("-")[0]]
        d = d[(d["start"] >= since) & (d["start"] < until) & (d["start"] < clock["now"].timestamp() * 1000)]
        return [{k: str(v) for k, v in r.items()} for r in d.head(1000).to_dict("records")]

    def fake_tickers(region):
        t = int(idx.searchsorted(clock["now"].floor("h")))
        return [{"symbol": f"{c}/USD", "bid": str(md["open"][c].iloc[t] * 0.999),
                 "ask": str(md["open"][c].iloc[t] * 1.001)} for c in md["close"].columns
                if np.isfinite(md["open"][c].iloc[t])] + [{"symbol": "BTC/GBP", "bid": "1", "ask": "1"}]

    old = api.candles, api.tickers
    api.candles, api.tickers = fake_candles, fake_tickers
    try:
        pdir = os.path.join(tmp, "paper_live")
        oks = []
        for t in range(H, H + 48):
            clock["now"] = idx[t] + pd.Timedelta(seconds=90)
            tr = P.PaperTrader(paper_dir=pdir, data_dir=live_dir, profile="standard",
                               strategy="breakout", params=params, capital=135.0)
            oks.append(tr.cycle(clock["now"]))
        grown = len(pd.read_csv(os.path.join(live_dir, "BTC.csv")))
        sp = pd.read_csv(os.path.join(live_dir, "spreads.csv"))
        results.append(check("live code path: downloads new candles and runs 48 hourly cycles",
                             all(oks) and grown == H + 47 and not os.path.exists("data"),
                             f"(BTC candles {grown}, cycles ok {sum(oks)}/48)"))
        results.append(check("live code path: daily spread refresh uses live bid/ask",
                             np.allclose(sp["spread_pct"], 0.2, atol=1e-6)))
    finally:
        api.candles, api.tickers = old

    # 5. the dashboard builds from a paper account, and before one exists
    import json as _json
    import dashboard as Dsh
    ok = True
    for pd_ in [os.path.join(tmp, "paper_standard"), os.path.join(tmp, "no_account")]:
        try:
            path = Dsh.build(os.path.join(tmp, "site"), data, pd_, fx=None)
            html = open(path, encoding="utf-8").read()
            blob = html.split('<script id="data" type="application/json">')[1].split("</script>")[0]
            d = _json.loads(blob.replace("<\\/", "</"))
            ok &= len(d["coins"]) == len(md["close"].columns) and "/*__DATA__*/" not in html
        except Exception as e:
            print(e)
            ok = False
    results.append(check("dashboard builds (with and without a paper account)", ok))

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL PAPER TESTS PASSED" if all(results) else "\nSOME PAPER TESTS FAILED")
    return all(results)


if __name__ == "__main__":
    main()
