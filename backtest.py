"""
Step 2: compare the two strategies on the downloaded data.

  python backtest.py

What it does:
  1. Splits history into TRAIN (first 65%) and TEST (last 35%).
  2. Tries every setting in BREAKOUT_GRID / ROTATION_GRID on TRAIN only.
  3. Takes the best TRAIN setting of each strategy and runs it on TEST,
     which is data it has never seen, so the result is a fair estimate.
  4. Compares both against simply holding BTC or an equal mix of coins.
Reports go to the results/ folder (summary.txt, equity.png, trade lists).
"""
import glob
import itertools
import os
import sys

import numpy as np
import pandas as pd

import config as C
from book import Book
from strategies import STRATEGIES

HOURS_PER_YEAR = 24 * 365


# ------------------------------------------------------------------ data
def load_data(data_dir=C.DATA_DIR, verbose=True):
    frames = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
        coin = os.path.splitext(os.path.basename(path))[0]
        if coin == "spreads":
            continue
        df = pd.read_csv(path)
        if len(df) < 500:
            if verbose:
                print(f"  skipping {coin}: only {len(df)} candles")
            continue
        df.index = pd.to_datetime(df["start"], unit="ms", utc=True)
        frames[coin] = df[["open", "high", "low", "close", "volume"]].astype(float)
    if not frames:
        sys.exit(f"No data found in '{data_dir}'. Run: python download_data.py")
    idx = sorted(set().union(*[f.index for f in frames.values()]))
    idx = pd.DatetimeIndex(idx)
    idx = pd.date_range(idx[0], idx[-1], freq="h")
    md = {k: pd.DataFrame({c: f[k].reindex(idx) for c, f in frames.items()})
          for k in ["open", "high", "low", "close", "volume"]}
    md = fill_gaps(md, verbose)
    return md


def fill_gaps(md, verbose=True):
    """Missing candles inside a coin's life (exchange maintenance, no data) are
    filled as flat candles at the last close with zero volume. Without this, one
    missing hour would blank out a 50-day average for 50 days."""
    c = md["close"]
    alive = c.ffill().notna() & c.bfill().notna()        # between first and last real candle
    missing = c.isna() & alive
    if missing.values.any():
        filled_close = c.ffill()
        for k in ["open", "high", "low", "close"]:
            md[k] = md[k].where(~missing, filled_close)
        if verbose:
            n = missing.sum()
            worst = n[n > 0].sort_values(ascending=False)
            print(f"  filled {int(n.sum())} missing hourly candles "
                  f"(most: {', '.join(f'{k} {v}' for k, v in worst.head(3).items())})")
    md["volume"] = md["volume"].fillna(0.0)
    return md


def load_half_spreads(data_dir=C.DATA_DIR):
    path = os.path.join(data_dir, "spreads.csv")
    if not os.path.exists(path):
        return {}
    s = pd.read_csv(path)
    return {r.coin: r.spread_pct / 100 / 2 for r in s.itertuples() if pd.notna(r.spread_pct)}


def base_cfg(half_spread=None, **over):
    cfg = {"capital": C.START_CAPITAL_USD, "fee": C.FEE_PER_SIDE, "slippage": C.SLIPPAGE,
           "half_spread": half_spread or {}, "default_half_spread": C.DEFAULT_HALF_SPREAD,
           "max_spread": C.MAX_SPREAD, "min_volume": C.MIN_24H_VOLUME_USD,
           "regime_hours": C.BTC_REGIME_HOURS, "daily_loss_limit": C.DAILY_LOSS_LIMIT,
           "min_order": C.MIN_ORDER_USD,
           # engine behaviour (overridden by a profile)
           "risk_per_trade": None, "position_cap": 1.0,
           "max_adds": 0, "add_atr": 2.0, "add_fraction": 0.5,
           "breadth_min": 0.0, "lock_gain": None, "lock_atr": 2.0}
    cfg.update(over)
    return cfg


ENGINE_KEYS = ["risk_per_trade", "position_cap", "max_adds", "add_atr", "add_fraction",
               "breadth_min", "lock_gain", "lock_atr"]


def profile_cfg(cfg, profile):
    prof = C.PROFILES[profile]
    return dict(cfg, **{k: prof[k] for k in ENGINE_KEYS if k in prof})


# ------------------------------------------------------------------ engine
_ARRAY_CACHE = []


def _arrays(md):
    """numpy copies of the price data, cached so repeated runs don't re-copy."""
    for frame, arrs in _ARRAY_CACHE:
        if frame is md["close"]:
            return arrs
    idx = md["close"].index
    days = np.asarray((idx - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(days=1))
    arrs = tuple(md[k].to_numpy() for k in ["open", "high", "low", "close"]) + (days,)
    _ARRAY_CACHE.append((md["close"], arrs))
    del _ARRAY_CACHE[:-3]
    return arrs


def run(md, strat, cfg, t0=0, t1=None, prepared=False):
    """Simulate a portfolio from bar t0 to t1. Signals come from the close of
    bar t; orders fill at the open of bar t+1; stops are checked against each
    bar's low. Returns (equity Series, trades DataFrame).
    All trading rules live in book.Book, shared with the paper/live bot."""
    if not prepared:
        strat.prepare(md, cfg)
    coins = list(md["close"].columns)
    O, H, L, Cl, days = _arrays(md)
    T = len(Cl) if t1 is None else t1
    hs = np.array([cfg["half_spread"].get(c, cfg["default_half_spread"]) for c in coins])
    idx = md["close"].index
    book = Book(coins, cfg, strat.stop_atr, strat.max_positions)
    eq = np.full(T - t0, np.nan)
    atr = strat.atr
    for t in range(t0, T):
        when = idx[t]
        if book.pend_sell or book.pend_buy or book.pend_add:
            book.execute(t, when, O[t], hs, atr[t - 1])        # 1) fill at this open
        if book.pos:
            book.stops_from_candle(when, L[t], O[t], hs)       # 2) stops vs this low
        eq[t - t0] = book.mark(Cl[t], atr[t], days[t])         # 3) mark at the close
        if t < T - 1:
            book.plan(t, strat, Cl[t], atr[t])                 # 4) decide for next bar
    return pd.Series(eq, index=idx[t0:T]), pd.DataFrame(book.trades)


# ------------------------------------------------------------------ metrics
def metrics(eq, trades=None):
    eq = eq.dropna()
    if len(eq) < 2:
        return {}
    r = eq.pct_change().dropna()
    years = len(eq) / HOURS_PER_YEAR
    total = eq.iloc[-1] / eq.iloc[0] - 1
    dd = (eq / eq.cummax() - 1).min()
    m = {"total_return_%": total * 100,
         "annual_return_%": ((1 + total) ** (1 / years) - 1) * 100 if total > -1 else -100,
         "max_drawdown_%": dd * 100,
         "sharpe": r.mean() / r.std() * np.sqrt(HOURS_PER_YEAR) if r.std() > 0 else 0.0}
    m["return_over_dd"] = m["annual_return_%"] / abs(m["max_drawdown_%"]) if dd < 0 else 0.0
    if trades is not None:
        n = len(trades)
        m["trades"] = n
        if n:
            w = trades["pnl_usd"] > 0
            m["win_rate_%"] = w.mean() * 100
            gl = -trades.loc[~w, "pnl_usd"].sum()
            m["profit_factor"] = trades.loc[w, "pnl_usd"].sum() / gl if gl > 0 else np.inf
            m["avg_trade_%"] = trades["return_pct"].mean()
            m["avg_hours_held"] = trades["hours_held"].mean()
            m["trades_per_week"] = n / (len(eq) / (24 * 7))
    return m


def benchmarks(md, t0, t1, capital):
    c = md["close"].iloc[t0:t1].ffill()
    out = {}
    if "BTC" in c:
        b = c["BTC"].dropna()
        out["Hold BTC"] = capital * b / b.iloc[0]
    first = c.iloc[0]
    have = first.dropna().index
    basket = (c[have] / first[have]).mean(axis=1)
    out["Hold equal mix"] = capital * basket
    return out


# ------------------------------------------------------------------ optimise
def grid_search(md, name, grid, cfg, t0, t1):
    rows = []
    keys = list(grid)
    defaults = C.BREAKOUT_DEFAULT if name == "breakout" else C.ROTATION_DEFAULT
    for combo in itertools.product(*grid.values()):
        params = dict(defaults, **dict(zip(keys, combo)))
        eq, tr = run(md, STRATEGIES[name](**params), cfg, t0, t1)
        rows.append({**dict(zip(keys, combo)), **metrics(eq, tr)})
    df = pd.DataFrame(rows)
    ok = df[df.get("trades", 0) >= C.MIN_TRADES_TO_RANK]
    best = (ok if len(ok) else df).sort_values("return_over_dd", ascending=False).iloc[0]
    return df, dict(defaults, **{k: type(grid[k][0])(best[k]) for k in keys})


def _fmt(m):
    keys = ["total_return_%", "annual_return_%", "max_drawdown_%", "sharpe", "trades",
            "win_rate_%", "profit_factor", "avg_trade_%", "avg_hours_held", "trades_per_week"]
    return {k: (round(m[k], 2) if isinstance(m.get(k), (float, np.floating)) else m.get(k, ""))
            for k in keys}


def main(data_dir=C.DATA_DIR, results_dir=C.RESULTS_DIR, make_chart=True):
    os.makedirs(results_dir, exist_ok=True)
    print("Loading data...")
    md = load_data(data_dir)
    cfg = base_cfg(load_half_spreads(data_dir))
    T = len(md["close"])
    split = int(T * C.TRAIN_FRACTION)
    idx = md["close"].index
    print(f"{md['close'].shape[1]} coins, {T} hourly bars ({idx[0]:%Y-%m-%d} to {idx[-1]:%Y-%m-%d})")
    print(f"TRAIN: {idx[0]:%Y-%m-%d} to {idx[split-1]:%Y-%m-%d}   TEST: {idx[split]:%Y-%m-%d} to {idx[-1]:%Y-%m-%d}\n")

    summary, curves, chosen = {}, {}, {}
    for profile in C.PROFILES:
        pcfg = profile_cfg(cfg, profile)
        for name in ["breakout", "rotation"]:
            grid = C.PROFILES[profile][f"{name}_grid"]
            key = f"{profile} {name}"
            print(f"Optimising {key} on TRAIN ({np.prod([len(v) for v in grid.values()])} settings)...")
            gtable, best = grid_search(md, name, grid, pcfg, 0, split)
            gtable.to_csv(os.path.join(results_dir, f"grid_{profile}_{name}.csv"), index=False)
            chosen[key] = best
            for label, (a, b) in {"TRAIN": (0, split), "TEST": (split, T)}.items():
                eq, tr = run(md, STRATEGIES[name](**best), pcfg, a, b)
                summary[(key, label)] = _fmt(metrics(eq, tr))
                if label == "TEST":
                    curves[key] = eq
                    tr.to_csv(os.path.join(results_dir, f"trades_{profile}_{name}_test.csv"), index=False)
            # sensitivity: does it survive zero-fee (maker) and double costs?
            for tag, over in {"TEST maker 0% fee": {"fee": 0.0},
                              "TEST double costs": {"fee": cfg["fee"] * 2,
                                                    "slippage": cfg["slippage"] * 2}}.items():
                eq, tr = run(md, STRATEGIES[name](**best), dict(pcfg, **over), split, T)
                summary[(key, tag)] = _fmt(metrics(eq, tr))

    for bname, beq in benchmarks(md, split, T, cfg["capital"]).items():
        summary[(bname, "TEST")] = _fmt(metrics(beq))
        curves[bname] = beq

    table = pd.DataFrame(summary).T
    table.index.names = ["strategy", "period"]
    test_only = table.xs("TEST", level="period")
    lines = ["REVOLUT X MULTI-COIN BACKTEST",
             f"Data: {md['close'].shape[1]} coins, {idx[0]:%Y-%m-%d} to {idx[-1]:%Y-%m-%d}; "
             f"TEST starts {idx[split]:%Y-%m-%d}",
             f"Costs per fill: fee {cfg['fee']*100:.2f}% + slippage {cfg['slippage']*100:.2f}% + half the coin's spread",
             f"Start capital ${cfg['capital']:,.0f}", "",
             "Chosen settings (best on TRAIN by annual return / max drawdown):"]
    for k, v in chosen.items():
        lines.append(f"  {k}: {v}")
    cols = ["total_return_%", "max_drawdown_%", "sharpe", "trades", "win_rate_%"]
    lines += ["", "=== TEST PERIOD AT A GLANCE (this is what counts) ===",
              test_only[cols].to_string(), "",
              "=== FULL DETAIL ===", table.to_string(), "",
              "How to read this: judge on the TEST rows only. A strategy is worth paper-trading",
              "if TEST beats holding BTC on return/drawdown, stays positive with double costs,",
              "and has a smaller max drawdown than you are prepared to sit through."]
    text = "\n".join(lines)
    with open(os.path.join(results_dir, "summary.txt"), "w") as f:
        f.write(text)
    print("\n" + text)

    if make_chart:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(11, 5.5))
            for k, s in curves.items():
                bench = k.startswith("Hold")
                ax.plot(s.index, s.values, label=k, lw=1.0 if bench else 1.5,
                        ls="--" if bench else ("-" if k.startswith("aggressive") else "-."))
            ax.set_yscale("log")
            ax.set_title("TEST period: equity (log scale)")
            ax.set_ylabel("USD")
            ax.legend()
            ax.grid(alpha=0.3)
            fig.tight_layout()
            fig.savefig(os.path.join(results_dir, "equity.png"), dpi=120)
            print(f"\nChart saved to {results_dir}/equity.png")
        except ImportError:
            print("(install matplotlib to get the chart)")
    return table


if __name__ == "__main__":
    main()
