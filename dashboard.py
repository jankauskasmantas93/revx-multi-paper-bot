"""
Builds the dashboard: one web page showing the paper account, what the bot
holds, each coin's price chart and how close every coin is to a buy signal.

  python dashboard.py            build site/index.html and open it in your browser
  python dashboard.py --no-open  just build it (used by the cloud runner)

The page is a single file with everything inside it (data, charts, styles),
so it works offline, from a folder, or hosted on GitHub Pages. When it can,
it also refreshes coin prices live from Revolut X every minute.
"""
import argparse
import json
import math
import os
import webbrowser

import numpy as np
import pandas as pd

import backtest as B
import config as C
from strategies import STRATEGIES, common_filters

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "dashboard_template.html")


def _sig(x, digits=6):
    """Round to significant digits so the page stays small."""
    if x is None or not np.isfinite(x) or x == 0:
        return 0 if x == 0 else None
    return float(f"{x:.{digits}g}")


def _ts(t):
    return int(pd.Timestamp(t).timestamp())


def _candles(df, rule, n):
    """df: one coin's 1h OHLCV (index = bar start). Resample to rule; last n bars."""
    if rule != "1h":
        df = df.resample(rule, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    df = df.dropna(subset=["close"]).tail(n)
    return [[_ts(t), _sig(r.open), _sig(r.high), _sig(r.low), _sig(r.close), _sig(r.volume, 4)]
            for t, r in zip(df.index, df.itertuples())]


def _fx_gbp():
    """USD per 1 GBP from Revolut X (BTC/USD divided by BTC/GBP), or None."""
    try:
        import revx_public as api
        t = {x["symbol"]: x for x in api.tickers(C.REGION)}
        usd = (float(t["BTC/USD"]["bid"]) + float(t["BTC/USD"]["ask"])) / 2
        gbp = (float(t["BTC/GBP"]["bid"]) + float(t["BTC/GBP"]["ask"])) / 2
        return usd / gbp if gbp > 0 else None
    except Exception:
        return None


def _raw_data(data_dir=C.DATA_DIR, paper_dir=C.PAPER_DIR, fx=None):
    md = B.load_data(data_dir, verbose=False)
    close, high = md["close"], md["high"]
    idx = close.index
    coins = list(close.columns)

    state = None
    sp = os.path.join(paper_dir, "state.json")
    if os.path.exists(sp):
        with open(sp) as f:
            state = json.load(f)
    if state:
        profile, strat_name, params = state["profile"], state["strategy"], state["params"]
    else:
        from paper_trader import choose_params
        profile, strat_name = C.PAPER_PROFILE, C.PAPER_STRATEGY
        params, _ = choose_params()
    cfg = B.profile_cfg(B.base_cfg(B.load_half_spreads(data_dir)), profile)
    strat = STRATEGIES[strat_name](**params)
    strat.prepare(md, cfg)
    liquid, regime = common_filters(md, cfg)
    T = len(idx) - 1
    lookback = params.get("lookback", 480)

    # market filter
    btc = close["BTC"] if "BTC" in close else None
    n = cfg["regime_hours"]
    market = {"on": bool(regime[T]), "hours": n}
    if btc is not None:
        sma = btc.rolling(n, min_periods=n).mean()
        market.update(btc=_sig(btc.iloc[-1]), btc_sma=_sig(sma.iloc[-1]))
    sma_all = close.rolling(n, min_periods=n).mean()
    above = (close.iloc[-1] > sma_all.iloc[-1])
    counted = sma_all.iloc[-1].notna()
    market.update(breadth_up=int((above & counted).sum()), breadth_n=int(counted.sum()))

    # positions
    held = state["book"]["positions"] if state and state.get("book") else {}
    positions = []
    for coin, p in held.items():
        last = float(close[coin].dropna().iloc[-1]) if coin in close else float("nan")
        value = p["qty"] * last
        positions.append({
            "coin": coin, "qty": p["qty"], "entry_time": _ts(p["entry_time"]),
            "avg": _sig(p["entry_px"]), "last": _sig(last), "value": value, "cost": p["cost"],
            "pnl": value - p["cost"], "pnl_pct": (value / p["cost"] - 1) * 100,
            "stop": _sig(p["stop"]), "stop_pct": (p["stop"] / last - 1) * 100, "adds": p["adds"],
            "hours": int((idx[-1] + pd.Timedelta(hours=1) - pd.Timestamp(p["entry_time"]))
                         / pd.Timedelta(hours=1))})
    positions.sort(key=lambda r: -r["value"])

    # trades, orders, markers
    def read_csv(name):
        path = os.path.join(paper_dir, name)
        return pd.read_csv(path) if os.path.exists(path) else pd.DataFrame()
    trades, orders, eqdf = read_csv("trades.csv"), read_csv("orders.csv"), read_csv("equity.csv")
    markers = {c: [] for c in coins}
    for r in orders.itertuples() if len(orders) else []:
        kind = {"BUY": "buy", "ADD": "add", "SELL": "sell", "STOP SELL": "stop"}.get(r.action)
        if kind and r.coin in markers:
            markers[r.coin].append({"t": _ts(r.time), "type": kind, "price": _sig(r.price)})

    spreads = B.load_half_spreads(data_dir)
    vol24 = (md["volume"] * close).iloc[-24:].sum()
    hh_next = high.rolling(lookback, min_periods=lookback).max()     # level the next close must beat
    out_coins = {}
    for i, c in enumerate(coins):
        s = close[c].dropna()
        if len(s) < 30:
            continue
        last = float(s.iloc[-1])
        ch = lambda h: (last / float(s.iloc[-1 - h]) - 1) * 100 if len(s) > h else None
        trig = hh_next[c].iloc[-1]
        dist = (trig / last - 1) * 100 if np.isfinite(trig) else None
        if c in held:
            status = "holding"
        elif not liquid[T, i]:
            status = "excluded"
        elif not regime[T]:
            status = "filter_off"
        elif strat.entry[T, i]:
            status = "signal"
        elif dist is not None and dist <= 3:
            status = "near"
        else:
            status = "waiting"
        df1h = md["open"][[c]].rename(columns={c: "open"}).assign(
            high=high[c], low=md["low"][c], close=close[c], volume=md["volume"][c]).dropna(subset=["close"])
        spark = s.iloc[-7 * 24::4]
        out_coins[c] = {
            "last": _sig(last), "ch24": ch(24), "ch7": ch(24 * 7), "ch30": ch(24 * 30),
            "trigger": _sig(trig) if np.isfinite(trig) else None, "dist": dist, "status": status,
            "spread": spreads.get(c, cfg["default_half_spread"]) * 2 * 100,
            "vol24": float(vol24.get(c, 0.0)),
            "spark": [_sig(x, 5) for x in spark],
            "c1h": _candles(df1h, "1h", 24 * 21),
            "c4h": _candles(df1h, "4h", 6 * 90),
            "c1d": _candles(df1h, "1D", 365),
            "markers": markers.get(c, []),
        }

    # equity vs holding BTC
    equity = {"bot": [], "btc": []}
    kpi = {}
    start_usd = state["start_usd"] if state else C.PAPER_START_USD
    if state and len(eqdf):
        e = eqdf.drop_duplicates("bar")
        e = pd.Series(e["equity"].values, index=pd.to_datetime(e["bar"], utc=True))
        step = max(1, len(e) // 1500)
        es = e.iloc[::step]
        if es.index[-1] != e.index[-1]:
            es = pd.concat([es, e.iloc[-1:]])
        equity["bot"] = [[_ts(t), round(float(v), 4)] for t, v in es.items()]
        if btc is not None:
            b = btc.reindex(e.index).ffill()
            b0 = float(btc.loc[:e.index[0]].dropna().iloc[-1])
            bs = (b / b0 * start_usd).reindex(es.index)
            equity["btc"] = [[_ts(t), round(float(v), 4)] for t, v in bs.items() if np.isfinite(v)]
        kpi = {"max_dd": float((e / e.cummax() - 1).min() * 100),
               "btc_ret": float((equity["btc"][-1][1] / start_usd - 1) * 100) if equity["btc"] else None}
    eq_now = state.get("equity", start_usd) if state else start_usd

    tr = []
    for r in (trades.tail(100).iloc[::-1].itertuples() if len(trades) else []):
        tr.append({"coin": r.coin, "entry": _ts(r.entry_time), "exit": _ts(r.exit_time),
                   "entry_price": _sig(r.entry_price), "exit_price": _sig(r.exit_price),
                   "pnl": float(r.pnl_usd), "ret": float(r.return_pct), "reason": r.exit_reason,
                   "hours": int(r.hours_held)})
    od = []
    for r in (orders.tail(100).iloc[::-1].itertuples() if len(orders) else []):
        od.append({"t": _ts(r.time), "action": r.action, "coin": r.coin, "price": _sig(r.price),
                   "usd": None if pd.isna(r.usd) else float(r.usd),
                   "pnl": None if pd.isna(r.pnl_usd) else float(r.pnl_usd)})

    return {
        "meta": {
            "generated": _ts(pd.Timestamp.now(tz="UTC")), "last_bar": _ts(idx[-1]),
            "last_run": _ts(state["last_run"]) if state and state.get("last_run") else None,
            "started": _ts(state["started_bar"]) if state and state.get("started_bar") else None,
            "has_account": bool(state and state.get("last_bar")),
            "profile": profile, "strategy": strat_name, "params": params,
            "start_usd": start_usd, "equity": eq_now,
            "cash": state["book"]["cash"] if state and state.get("book") else start_usd,
            "fx_gbp": fx, "region": C.REGION, "quote": C.QUOTE,
            "max_positions": params.get("max_positions"),
            "lookback_days": round(lookback / 24, 1),
        },
        "market": market, "kpi": kpi, "positions": positions, "coins": out_coins,
        "equity": equity, "trades": tr, "orders": od,
    }


def build(out_dir="site", data_dir=C.DATA_DIR, paper_dir=C.PAPER_DIR, fx="auto"):
    if fx == "auto":
        fx = _fx_gbp()
    data = build_data(data_dir, paper_dir, fx)
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    payload = json.dumps(data, separators=(",", ":"), allow_nan=False, default=lambda o: None)
    payload = payload.replace("</", "<\\/")
    html = html.replace("/*__DATA__*/null", payload)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def _clean(o):
    """json can't hold NaN/inf: turn them into null."""
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def build_data(data_dir=C.DATA_DIR, paper_dir=C.PAPER_DIR, fx=None):
    """Everything the page shows, as plain JSON-safe data."""
    return _clean(_raw_data(data_dir, paper_dir, fx))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--out", default="site")
    a = ap.parse_args()
    path = build(a.out)
    print(f"Dashboard written to {path}")
    if not a.no_open:
        webbrowser.open("file://" + os.path.abspath(path))


if __name__ == "__main__":
    main()
