"""
Walk-forward test on long history. This is the fairest test the project has.

  python walk_forward.py

How it works (the defaults are 12 months to tune, 3 months to trade):
  - Tune on Jan-Dec 2020, then trade Jan-Mar 2021 with the winning settings
  - Tune on Apr 2020-Mar 2021, then trade Apr-Jun 2021 ... and so on to today
  - Only the traded months, which the settings never saw, are joined into the result
It also runs every setting unchanged across all the traded months. If most
settings make money, the edge is real. If only one does, it was luck.

Reads data_long/ (run download_history.py first). Writes results_wf/.
Takes roughly 10-30 minutes depending on your laptop.
"""
import itertools
import os
import sys
import time

import numpy as np
import pandas as pd

import backtest as B
import config as C
from strategies import STRATEGIES


def make_folds(idx, train_m, test_m):
    """List of (train_start, train_end, test_start, test_end) as bar positions."""
    first = idx[0].normalize().replace(day=1)
    test_start = first + pd.DateOffset(months=train_m + (1 if idx[0] > first else 0))
    folds = []
    while test_start < idx[-1]:
        test_end = test_start + pd.DateOffset(months=test_m)
        train_start = test_start - pd.DateOffset(months=train_m)
        a, b, c, d = (idx.searchsorted(x) for x in [train_start, test_start, test_start, test_end])
        if d - c > 24 * 7:                       # skip a last fold shorter than a week
            folds.append((a, b, c, min(d, len(idx))))
        test_start = test_end
    return folds


def stitch(pieces, capital):
    """Join per-fold equity curves, each continuing from where the last ended."""
    out, level = [], capital
    for eq in pieces:
        eq = eq.dropna()
        if eq.empty:
            continue
        seg = eq / eq.iloc[0] * level
        out.append(seg)
        level = seg.iloc[-1]
    return pd.concat(out) if out else pd.Series(dtype=float)


def fold_benchmarks(md, folds):
    c = md["close"].ffill()
    btc, mix = [], []
    for _, _, a, b in folds:
        seg = c.iloc[a:b]
        if "BTC" in seg:
            btc.append(seg["BTC"])
        have = seg.iloc[0].dropna().index
        mix.append((seg[have] / seg[have].iloc[0]).mean(axis=1))
    return btc, mix


def yearly(eq):
    y = eq.groupby(eq.index.year).agg(["first", "last"])
    prev = y["last"].shift(1).fillna(y["first"])
    return ((y["last"] / prev - 1) * 100).round(1)


def evaluate(md, cfg, name, grid, folds, min_trades=C.MIN_TRADES_TO_RANK):
    """Runs every setting on every fold. Returns per-fold choice and curves."""
    keys = list(grid)
    defaults = C.BREAKOUT_DEFAULT if name == "breakout" else C.ROTATION_DEFAULT
    combos = list(itertools.product(*grid.values()))
    train_score = np.full((len(combos), len(folds)), -np.inf)
    test_eq, test_tr = {}, {}
    for ci, combo in enumerate(combos):
        params = dict(defaults, **dict(zip(keys, combo)))
        strat = STRATEGIES[name](**params)
        strat.prepare(md, cfg)
        for fi, (a, b, c, d) in enumerate(folds):
            eq, tr = B.run(md, strat, cfg, a, b, prepared=True)
            m = B.metrics(eq, tr)
            if m and m.get("trades", 0) >= min_trades:
                train_score[ci, fi] = m["return_over_dd"]
            elif m:
                train_score[ci, fi] = -1e6 + m.get("trades", 0)   # too few trades: last resort
            eq, tr = B.run(md, strat, cfg, c, d, prepared=True)
            test_eq[ci, fi], test_tr[ci, fi] = eq, tr
        print(f"    setting {ci + 1}/{len(combos)} done", end="\r", flush=True)
    print()
    chosen = train_score.argmax(axis=0)
    return keys, combos, chosen, test_eq, test_tr


def main(data_dir=C.LONG_DATA_DIR, out_dir="results_wf"):
    os.makedirs(out_dir, exist_ok=True)
    t_start = time.time()
    print("Loading long history...")
    md = B.load_data(data_dir)
    cfg = B.base_cfg(B.load_half_spreads(data_dir))
    idx = md["close"].index
    folds = make_folds(idx, C.WF_TRAIN_MONTHS, C.WF_TEST_MONTHS)
    if len(folds) < 2:
        sys.exit("Not enough history for a walk-forward test. Run download_history.py.")
    print(f"{md['close'].shape[1]} coins, {idx[0]:%Y-%m-%d} to {idx[-1]:%Y-%m-%d}")
    print(f"{len(folds)} folds: trading {idx[folds[0][2]]:%Y-%m-%d} to {idx[folds[-1][3] - 1]:%Y-%m-%d} "
          f"(tune {C.WF_TRAIN_MONTHS} months, trade {C.WF_TEST_MONTHS} months)\n")

    btc_parts, mix_parts = fold_benchmarks(md, folds)
    curves = {"Hold BTC": stitch(btc_parts, cfg["capital"]),
              "Hold equal mix": stitch(mix_parts, cfg["capital"])}
    summary, fold_rows, robust_rows, all_trades = {}, [], [], {}
    fold_ret = lambda s: (s.dropna().iloc[-1] / s.dropna().iloc[0] - 1) * 100 if s.notna().sum() > 1 else 0.0

    for profile in C.WF_PROFILES:
        pcfg = B.profile_cfg(cfg, profile)
        for name in C.WF_STRATEGIES:
            key = f"{profile} {name}"
            grid = C.PROFILES[profile][f"{name}_grid"]
            print(f"  {key}: {np.prod([len(v) for v in grid.values()])} settings x {len(folds)} folds")
            keys, combos, chosen, test_eq, test_tr = evaluate(md, pcfg, name, grid, folds,
                                                             C.PROFILES[profile].get("min_trades", C.MIN_TRADES_TO_RANK))

            # 1) the walk-forward result: re-tuned every fold
            pieces = [test_eq[chosen[fi], fi] for fi in range(len(folds))]
            eq = stitch(pieces, cfg["capital"])
            tr = pd.concat([test_tr[chosen[fi], fi] for fi in range(len(folds))], ignore_index=True)
            curves[key], all_trades[key] = eq, tr
            m = B.metrics(eq, tr)
            wins = pos = 0
            for fi, (a, b, c, d) in enumerate(folds):
                r = fold_ret(pieces[fi])
                rb, rm = fold_ret(btc_parts[fi]) if btc_parts else np.nan, fold_ret(mix_parts[fi])
                wins += r > rb
                pos += r > 0
                ftr = test_tr[chosen[fi], fi]
                fold_rows.append({"strategy": key, "traded_from": f"{idx[c]:%Y-%m-%d}",
                                  "traded_to": f"{idx[d - 1]:%Y-%m-%d}",
                                  **{k: v for k, v in zip(keys, combos[chosen[fi]])},
                                  "return_%": round(r, 1),
                                  "max_dd_%": round(B.metrics(pieces[fi]).get("max_drawdown_%", 0), 1),
                                  "trades": len(ftr), "btc_%": round(rb, 1), "mix_%": round(rm, 1)})
            m["folds_beat_btc"] = f"{wins}/{len(folds)}"
            m["folds_positive"] = f"{pos}/{len(folds)}"
            summary[key] = m

            # 2) robustness: every setting held fixed across all traded months
            for ci, combo in enumerate(combos):
                e = stitch([test_eq[ci, fi] for fi in range(len(folds))], cfg["capital"])
                t = pd.concat([test_tr[ci, fi] for fi in range(len(folds))], ignore_index=True)
                rm = B.metrics(e, t)
                robust_rows.append({"strategy": key, **dict(zip(keys, combo)),
                                    "annual_return_%": round(rm.get("annual_return_%", 0), 1),
                                    "max_drawdown_%": round(rm.get("max_drawdown_%", 0), 1),
                                    "sharpe": round(rm.get("sharpe", 0), 2), "trades": rm.get("trades", 0)})

    for bname in ["Hold BTC", "Hold equal mix"]:
        summary[bname] = B.metrics(curves[bname])

    # ------------------------------------------------------------ report
    cols = ["total_return_%", "annual_return_%", "max_drawdown_%", "sharpe", "trades",
            "win_rate_%", "profit_factor", "trades_per_week", "folds_positive", "folds_beat_btc"]
    table = pd.DataFrame(summary).T.reindex(columns=cols)
    for c in cols[:8]:
        table[c] = pd.to_numeric(table[c], errors="coerce").round(2)
    robust = pd.DataFrame(robust_rows)
    rob_sum = robust.groupby("strategy").agg(
        settings=("annual_return_%", "size"),
        profitable=("annual_return_%", lambda s: f"{(s > 0).sum()}/{len(s)}"),
        median_annual_return_pct=("annual_return_%", "median"),
        worst_annual_return_pct=("annual_return_%", "min"),
        median_max_dd_pct=("max_drawdown_%", "median"))
    years = pd.DataFrame({k: yearly(v) for k, v in curves.items() if len(v)}).T
    years.columns = [str(c) for c in years.columns]

    pd.DataFrame(fold_rows).to_csv(os.path.join(out_dir, "folds.csv"), index=False)
    robust.to_csv(os.path.join(out_dir, "all_settings.csv"), index=False)
    for k, t in all_trades.items():
        t.to_csv(os.path.join(out_dir, f"trades_{k.replace(' ', '_')}.csv"), index=False)

    lines = ["WALK-FORWARD TEST (only months the settings were never tuned on)",
             f"Data: {md['close'].shape[1]} coins (Binance history, Revolut X costs), "
             f"trading {idx[folds[0][2]]:%Y-%m-%d} to {idx[folds[-1][3] - 1]:%Y-%m-%d}, {len(folds)} folds",
             f"Costs per fill: fee {cfg['fee']*100:.2f}% + slippage {cfg['slippage']*100:.2f}% "
             f"+ half the coin's Revolut X spread.  Start ${cfg['capital']:,.0f}", "",
             "=== RESULT ===", table.to_string(), "",
             "=== RETURN BY CALENDAR YEAR (%) ===", years.to_string(), "",
             "=== ROBUSTNESS: every setting held fixed for the whole period ===",
             rob_sum.round(1).to_string(), "",
             "How to read this:",
             " - RESULT: the walk-forward row is what the bot would realistically have done.",
             " - folds_positive / folds_beat_btc: how often each 3-month block made money / beat BTC.",
             " - ROBUSTNESS: if most settings are profitable, the idea works, not just one lucky setting.",
             " - folds.csv shows each 3-month block and the settings chosen for it.",
             " - Survivor bias: the coins are today's survivors, so all absolute returns",
             "   (especially 'Hold equal mix') are optimistic. Compare strategies to each other and to BTC."]
    text = "\n".join(lines)
    with open(os.path.join(out_dir, "summary.txt"), "w") as f:
        f.write(text)
    print("\n" + text)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, (ax, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                                      gridspec_kw={"height_ratios": [3, 1.3]})
        for k, s in curves.items():
            if not len(s):
                continue
            bench = k.startswith("Hold")
            ax.plot(s.index, s.values, label=k, lw=1.0 if bench else 1.5, ls="--" if bench else "-")
            ax2.plot(s.index, (s / s.cummax() - 1) * 100, lw=0.9, ls="--" if bench else "-")
        ax.set_yscale("log")
        ax.set_title("Walk-forward: out-of-sample equity (log scale)")
        ax.set_ylabel("USD")
        ax.legend(fontsize=9)
        ax.grid(alpha=0.3)
        ax2.set_ylabel("Drawdown %")
        ax2.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, "equity.png"), dpi=120)
    except ImportError:
        pass
    print(f"\nDone in {(time.time() - t_start) / 60:.1f} min. Reports are in '{out_dir}'.")


if __name__ == "__main__":
    main()
