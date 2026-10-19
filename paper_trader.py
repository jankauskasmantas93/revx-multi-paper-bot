"""
Paper trading: runs the Slow trend strategy on LIVE Revolut X prices with
FAKE money. No API key is needed and no real orders are ever sent.

  python paper_trader.py            run continuously (leave the window open)
  python paper_trader.py --once     run one hourly check and exit
  python paper_trader.py --reset    wipe the paper account and start again

Every hour, just after the hourly candle closes, it:
  1. downloads the newest candles from Revolut X (about 1 minute)
  2. applies each open position's trailing stop to the candle(s) since the last
     check, the same way a stop order resting on the exchange would fill
  3. updates the stops, then decides what to buy and sell using EXACTLY the
     same code as the backtest (book.py + strategies.py)
  4. "fills" those orders at the current live Revolut X price, including the
     real spread, the 0.09% fee and slippage
  5. saves everything to the paper/ folder

If the laptop sleeps or the window is closed, nothing is lost. When it runs
again it catches up on the missed hours (stops are applied to the missed
candles) and carries on.
To stop it: press Ctrl+C, or create a file called STOP in this folder.
To see how it's doing: python status.py
"""
import argparse
import json
import logging
import os
import sys
import time
import traceback

import numpy as np
import pandas as pd

import backtest as B
import config as C
from book import Book
from strategies import STRATEGIES

HOUR = pd.Timedelta(hours=1)
log = logging.getLogger("paper")


# ------------------------------------------------------------------ helpers
def utcnow():
    return pd.Timestamp.now(tz="UTC")


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


ORDER_COLS = ["time", "action", "coin", "price", "usd", "pnl_usd"]


def _append_csv(path, rows, columns=None):
    if not rows:
        return
    df = pd.DataFrame(rows)
    if columns:
        df = df.reindex(columns=columns)
    df.to_csv(path, mode="a", header=not os.path.exists(path), index=False)


def choose_params():
    """Settings the walk-forward picked most recently, else the fallback."""
    key = f"{C.PAPER_PROFILE} {C.PAPER_STRATEGY}"
    path = os.path.join("results_wf", "folds.csv")
    if C.PAPER_PARAMS == "auto" and os.path.exists(path):
        f = pd.read_csv(path)
        f = f[f["strategy"] == key]
        if len(f):
            last = f.iloc[-1]
            grid = C.PROFILES[C.PAPER_PROFILE][f"{C.PAPER_STRATEGY}_grid"]
            return {k: type(grid[k][0])(last[k]) for k in grid}, f"walk-forward (block from {last['traded_from']})"
    if isinstance(C.PAPER_PARAMS, dict):
        return dict(C.PAPER_PARAMS), "config.PAPER_PARAMS"
    return dict(C.PAPER_PARAMS_FALLBACK), "config.PAPER_PARAMS_FALLBACK"


# ------------------------------------------------------------------ live hooks
class LiveHooks:
    """Everything that touches the internet. Tests swap this for a fake."""

    def __init__(self, data_dir):
        self.data_dir = data_dir

    def coins(self):
        return sorted(os.path.splitext(f)[0] for f in os.listdir(self.data_dir)
                      if f.endswith(".csv") and f != "spreads.csv")

    def update_data(self, now):
        import download_data as D
        now_ms = int(now.timestamp() * 1000)
        failed = []
        for coin in self.coins():
            try:
                D.download_coin(coin, now_ms, self.data_dir)
            except Exception as e:                       # one bad coin must not stop the bot
                failed.append(f"{coin} ({e})")
        if failed:
            log.warning("candle update failed for: %s", ", ".join(failed))
        self._refresh_spreads()

    def _refresh_spreads(self):
        """Once a day, update each coin's measured spread (used by the spread filter)."""
        path = os.path.join(self.data_dir, "spreads.csv")
        if not os.path.exists(path) or time.time() - os.path.getmtime(path) < 86400:
            return
        try:
            s = pd.read_csv(path)
            live = self.tickers(list(s["coin"]))
            for r in s.itertuples():
                bid, ask = live.get(r.coin, (np.nan, np.nan))
                if bid > 0 and ask > 0:
                    s.loc[r.Index, "spread_pct"] = (ask - bid) / ((ask + bid) / 2) * 100
            s.to_csv(path, index=False)
        except Exception as e:
            log.warning("spread refresh failed: %s", e)

    def load_md(self):
        return B.load_data(self.data_dir, verbose=False)

    def tickers(self, coins):
        import revx_public as api
        out = {}
        for t in api.tickers(C.REGION):
            base, quote = t["symbol"].split("/")
            if quote == C.QUOTE:
                out[base] = (float(t.get("bid") or np.nan), float(t.get("ask") or np.nan))
        return out


# ------------------------------------------------------------------ trader
class PaperTrader:
    def __init__(self, paper_dir=C.PAPER_DIR, data_dir=C.DATA_DIR, hooks=None, profile=None,
                 strategy=None, params=None, capital=None):
        self.dir, self.data_dir = paper_dir, data_dir
        self.hooks = hooks or LiveHooks(data_dir)
        os.makedirs(paper_dir, exist_ok=True)
        self.state_path = os.path.join(paper_dir, "state.json")
        self.state = self._load_state()
        if self.state is None:                                # brand new paper account
            if params is None:
                params, source = choose_params()
            else:
                source = "given"
            self.state = {"version": 1, "created": utcnow().isoformat(),
                          "profile": profile or C.PAPER_PROFILE,
                          "strategy": strategy or C.PAPER_STRATEGY,
                          "params": params, "params_source": source,
                          "start_usd": capital or C.PAPER_START_USD,
                          "started_bar": None, "last_bar": None, "book": None}

    def _load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                return json.load(f)
        return None

    def _save_state(self):
        _atomic_write(self.state_path, json.dumps(self.state, indent=2, default=str))

    def cfg(self):
        base = B.base_cfg(B.load_half_spreads(self.data_dir), capital=self.state["start_usd"])
        return B.profile_cfg(base, self.state["profile"])

    # -------------------------------------------------------------- one cycle
    def cycle(self, now):
        """Process every closed candle not yet processed, then trade at live prices.
        Returns True when done, False when the data is not ready yet (retry later)."""
        st = self.state
        expected = now.floor("h") - HOUR                  # start time of the newest closed candle
        self.hooks.update_data(now)
        md = self.hooks.load_md()
        idx = md["close"].index
        if idx[-1] < expected or ("BTC" in md["close"] and
                                  not np.isfinite(md["close"]["BTC"].get(expected, np.nan))):
            log.warning("newest candle for %s not available yet (have up to %s)", expected, idx[-1])
            return False
        T = int(idx.searchsorted(expected, side="right"))   # ignore anything newer (shouldn't exist)
        if T < len(idx):
            md = {k: v.iloc[:T] for k, v in md.items()}
            idx = md["close"].index

        cfg = self.cfg()
        strat = STRATEGIES[st["strategy"]](**st["params"])
        strat.prepare(md, cfg)
        coins = list(md["close"].columns)
        book = Book(coins, cfg, strat.stop_atr, strat.max_positions)
        if st["book"]:
            book.load_dict(st["book"], idx)
        O, L, Cl = (md[k].to_numpy() for k in ["open", "low", "close"])
        days = np.asarray((idx - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(days=1))
        hs_snap = np.array([cfg["half_spread"].get(c, cfg["default_half_spread"]) for c in coins])

        if st["last_bar"] is None:
            first = T - 1                                    # first ever run: start from the newest candle
            st["started_bar"] = idx[first].isoformat()
        else:
            first = int(idx.searchsorted(pd.Timestamp(st["last_bar"]), side="right"))
        if first >= T:
            log.info("no new candle since %s", st["last_bar"])
            return True
        if T - first > 1:
            log.info("catching up on %d missed hours", T - first)

        closed, eq_rows, order_rows = [], [], []
        for t in range(first, T):
            if book.pos and st["last_bar"] is not None:
                closed += book.stops_from_candle(idx[t], L[t], O[t], hs_snap)
            eq = book.mark(Cl[t], strat.atr[t], days[t])
            eq_rows.append({"bar": idx[t].isoformat(), "equity": eq})

        # decide at the newest close, then fill at the live price right now
        t = T - 1
        book.plan(t, strat, Cl[t], strat.atr[t])
        if book.pend_sell or book.pend_buy or book.pend_add:
            live = self.hooks.tickers(coins)
            bid = np.array([live.get(c, (np.nan, np.nan))[0] for c in coins])
            ask = np.array([live.get(c, (np.nan, np.nan))[1] for c in coins])
            mid = (bid + ask) / 2
            hs_live = np.where(mid > 0, (ask - bid) / 2 / mid, np.nan)
            jump = np.abs(mid / Cl[t] - 1) > C.MAX_PRICE_JUMP
            for i in np.where(jump & np.isfinite(mid))[0]:
                if i in book.pend_sell or i in book.pend_buy or i in book.pend_add:
                    log.warning("%s: live price %.6g is >%.0f%% from last close %.6g; order skipped",
                                coins[i], mid[i], C.MAX_PRICE_JUMP * 100, Cl[t, i])
            mid = np.where(jump, np.nan, mid)
            for action, info in book.execute(t + 1, now, mid, hs_live, strat.atr[t]):
                if action == "SELL":
                    closed.append(info)
                    order_rows.append({"time": now.isoformat(), "action": "SELL", "coin": info["coin"],
                                       "price": info["exit_price"], "pnl_usd": round(info["pnl_usd"], 4)})
                else:
                    order_rows.append({"time": now.isoformat(), "action": action, "coin": info["coin"],
                                       "price": info["price"], "usd": round(info["usd"], 4)})
        for tr in closed:
            if tr["exit_reason"] == "stop":
                order_rows.append({"time": tr["exit_time"].isoformat(), "action": "STOP SELL",
                                   "coin": tr["coin"], "price": tr["exit_price"],
                                   "pnl_usd": round(tr["pnl_usd"], 4)})

        st["last_bar"] = idx[T - 1].isoformat()
        st["book"] = book.to_dict()
        st["last_run"] = now.isoformat()
        st["equity"] = book.equity_prev
        self._save_state()
        _append_csv(os.path.join(self.dir, "equity.csv"), eq_rows)
        _append_csv(os.path.join(self.dir, "orders.csv"), order_rows, ORDER_COLS)
        _append_csv(os.path.join(self.dir, "trades.csv"), [
            {**tr, "entry_time": tr["entry_time"].isoformat(), "exit_time": tr["exit_time"].isoformat()}
            for tr in closed])

        for r in order_rows:
            extra = f"  P&L ${r['pnl_usd']:+.2f}" if "pnl_usd" in r else f"  ${r.get('usd', 0):.2f}"
            log.info("%-9s %-6s at %.6g%s", r["action"], r["coin"], r["price"], extra)
        held = ", ".join(coins[i] for i in book.pos) or "none"
        log.info("candle %s done | equity $%.2f | cash $%.2f | holding: %s",
                 idx[T - 1].strftime("%Y-%m-%d %H:%M"), book.equity_prev, book.cash, held)
        return True


# ------------------------------------------------------------------ main loop
def setup_logging(paper_dir):
    os.makedirs(paper_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout),
              logging.FileHandler(os.path.join(paper_dir, "paper.log"), encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)


def run_forever(trader):
    done_hour, first_try = None, None
    log.info("paper trader running. Stop with Ctrl+C or by creating a file named STOP.")
    while True:
        if os.path.exists("STOP"):
            log.info("STOP file found, exiting (state is saved).")
            return
        now = utcnow()
        hour = now.floor("h")
        due = now >= hour + pd.Timedelta(seconds=C.CYCLE_DELAY_SECONDS)
        if due and done_hour != hour:
            try:
                ok = trader.cycle(now)
            except Exception:
                log.error("cycle failed, will retry:\n%s", traceback.format_exc())
                ok = False
            if ok or now - hour > pd.Timedelta(minutes=20):   # give up on this hour after 20 min
                done_hour = hour
            else:
                time.sleep(120)
                continue
        time.sleep(30)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one hourly check and exit")
    ap.add_argument("--reset", action="store_true", help="wipe the paper account and start again")
    a = ap.parse_args()
    if not os.path.exists(os.path.join(C.DATA_DIR, "spreads.csv")):
        sys.exit("No Revolut X data yet. Run: python download_data.py")
    if a.reset:
        if input("This deletes the paper account history. Type RESET to confirm: ").strip() != "RESET":
            sys.exit("Not reset.")
        for f in ["state.json", "equity.csv", "orders.csv", "trades.csv"]:
            p = os.path.join(C.PAPER_DIR, f)
            if os.path.exists(p):
                os.remove(p)
        print("Paper account reset.")
    setup_logging(C.PAPER_DIR)
    trader = PaperTrader()
    st = trader.state
    log.info("strategy: %s %s %s (settings from %s), paper pot $%.2f",
             st["profile"], st["strategy"], st["params"], st["params_source"],
             st.get("equity", st["start_usd"]))
    if a.once:
        trader.cycle(utcnow())
    else:
        try:
            run_forever(trader)
        except KeyboardInterrupt:
            log.info("stopped by you (state is saved).")


if __name__ == "__main__":
    main()
