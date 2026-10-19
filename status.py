"""
How is the paper account doing?   python status.py

Shows: equity and return since the start, compared with just holding BTC;
open positions with their stops; closed trades; and what the BACKTEST says
should have happened over the same hours. If paper and backtest drift far
apart, something is wrong (data, fills or code) and we should look before
going live.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

import backtest as B
import config as C
from strategies import STRATEGIES


def report(paper_dir=C.PAPER_DIR, data_dir=C.DATA_DIR, now=None):
    path = os.path.join(paper_dir, "state.json")
    if not os.path.exists(path):
        return "No paper account yet. Start one with: python paper_trader.py"
    st = json.load(open(path))
    if not st.get("last_bar"):
        return "Paper account created but no hour processed yet."
    now = now or pd.Timestamp.now(tz="UTC")
    eq = pd.read_csv(os.path.join(paper_dir, "equity.csv"))
    eq["bar"] = pd.to_datetime(eq["bar"], utc=True)
    eq = eq.drop_duplicates("bar").set_index("bar")["equity"]
    tpath = os.path.join(paper_dir, "trades.csv")
    trades = pd.read_csv(tpath) if os.path.exists(tpath) else pd.DataFrame()
    md = B.load_data(data_dir, verbose=False)
    close = md["close"]
    start_bar, last_bar = pd.Timestamp(st["started_bar"]), pd.Timestamp(st["last_bar"])
    start, equity = st["start_usd"], st["equity"]
    L = []
    L.append("PAPER ACCOUNT (fake money, live Revolut X prices)")
    L.append(f"Strategy: {st['profile']} {st['strategy']} {st['params']}")
    L.append(f"Running since {start_bar:%Y-%m-%d %H:%M} UTC, last candle {last_bar:%Y-%m-%d %H:%M} UTC")
    age = (now - pd.Timestamp(st["last_run"])) / pd.Timedelta(hours=1)
    if age > 2:
        L.append(f"!! WARNING: the bot last ran {age:.0f} hours ago. Is paper_trader.py still running?")
    L.append("")

    dd = (eq / eq.cummax() - 1).min() * 100 if len(eq) else 0.0
    L.append(f"Equity:   ${equity:,.2f}  (started ${start:,.2f})  {(equity / start - 1) * 100:+.1f}%")
    L.append(f"Worst drop so far: {dd:.1f}%")
    if "BTC" in close:
        b = close["BTC"].loc[start_bar:last_bar].dropna()
        if len(b) > 1:
            L.append(f"Holding BTC instead: {(b.iloc[-1] / b.iloc[0] - 1) * 100:+.1f}%")
    L.append("")

    pos = st["book"]["positions"]
    L.append(f"OPEN POSITIONS ({len(pos)}), cash ${st['book']['cash']:,.2f}")
    for coin, p in pos.items():
        last = close[coin].loc[:last_bar].dropna().iloc[-1] if coin in close else np.nan
        value = p["qty"] * last
        L.append(f"  {coin:7s} bought {p['entry_time'][:16]}  value ${value:7.2f}  "
                 f"P&L {(value / p['cost'] - 1) * 100:+6.1f}%  stop {p['stop']:.6g} "
                 f"({(p['stop'] / last - 1) * 100:+.1f}% away)" + (f"  adds {p['adds']}" if p["adds"] else ""))
    if not pos:
        L.append("  none (waiting for a coin to break out while the market filter is on)")
    L.append("")

    if len(trades):
        w = trades["pnl_usd"] > 0
        L.append(f"CLOSED TRADES: {len(trades)}  wins {w.sum()}  losses {(~w).sum()}  "
                 f"total P&L ${trades['pnl_usd'].sum():+.2f}")
        for r in trades.tail(10).itertuples():
            L.append(f"  {r.coin:7s} {r.entry_time[:16]} -> {r.exit_time[:16]}  {r.return_pct:+6.1f}%  "
                     f"${r.pnl_usd:+.2f}  ({r.exit_reason})")
    else:
        L.append("CLOSED TRADES: none yet")
    L.append("")

    # what the backtest says for the same hours
    idx = close.index
    t0, t1 = int(idx.searchsorted(start_bar)), int(idx.searchsorted(last_bar, side="right"))
    if t1 - t0 >= 2:
        cfg = B.profile_cfg(B.base_cfg(B.load_half_spreads(data_dir), capital=start), st["profile"])
        beq, btr = B.run(md, STRATEGIES[st["strategy"]](**st["params"]), cfg, t0, t1)
        L.append("BACKTEST OVER THE SAME HOURS (should be close to the paper account):")
        L.append(f"  backtest {(beq.iloc[-1] / start - 1) * 100:+.1f}%, {len(btr)} closed trades   |   "
                 f"paper {(equity / start - 1) * 100:+.1f}%, {len(trades)} closed trades")
        gap = abs(beq.iloc[-1] - equity) / start * 100
        if gap > 5:
            L.append(f"  !! paper and backtest differ by {gap:.1f}% of the pot. Send me status output to check.")
    return "\n".join(L)


if __name__ == "__main__":
    print(report())
