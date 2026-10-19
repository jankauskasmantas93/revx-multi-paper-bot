"""
Checks that the backtester is honest. Uses made-up price data, so it works
without internet.  Run:  python self_test.py   (all checks should say PASS)

1. No look-ahead: cutting off the future must not change any past result.
2. Costs matter: higher fees must give lower results, zero costs higher.
3. No over-spending: cash never goes negative, never more positions than allowed.
4. End-to-end: the full backtest report runs on a demo data folder.
"""
import os
import shutil
import tempfile

import numpy as np
import pandas as pd

import backtest as B
from strategies import STRATEGIES


def make_demo_data(folder, n_coins=12, hours=6000, seed=7):
    """Writes fake candle files that look like Revolut X downloads."""
    rng = np.random.default_rng(seed)
    os.makedirs(folder, exist_ok=True)
    start_ms = 1_700_000_000_000 - (1_700_000_000_000 % 3_600_000)
    market = np.cumsum(rng.normal(0, 0.006, hours) + 0.0002 * np.sin(np.arange(hours) / 700))
    names = ["BTC"] + [f"COIN{i}" for i in range(1, n_coins)]
    spreads = []
    for k, name in enumerate(names):
        beta = 1.0 if name == "BTC" else rng.uniform(0.8, 1.6)
        own = np.cumsum(rng.normal(0, 0.008, hours) + rng.normal(0, 0.0003)
                        * np.sign(np.sin(np.arange(hours) / rng.uniform(200, 900))))
        logp = np.log(100 * (k + 1)) + beta * market + own
        close = np.exp(logp)
        open_ = np.r_[close[0], close[:-1]]
        wig = np.abs(rng.normal(0, 0.004, hours))
        high = np.maximum(open_, close) * (1 + wig)
        low = np.minimum(open_, close) * (1 - wig)
        vol = rng.uniform(50, 500, hours) * 1000 / close
        late = 0 if k < 8 else int(rng.integers(500, 2500))  # some coins list later
        df = pd.DataFrame({"start": start_ms + np.arange(hours) * 3_600_000,
                           "open": open_, "high": high, "low": low, "close": close,
                           "volume": vol}).iloc[late:]
        df.to_csv(os.path.join(folder, f"{name}.csv"), index=False)
        spreads.append({"coin": name, "symbol": f"{name}/USD", "quote_volume_24h": 1e6,
                        "bid": 1, "ask": 1, "spread_pct": rng.uniform(0.02, 0.3)})
    pd.DataFrame(spreads).to_csv(os.path.join(folder, "spreads.csv"), index=False)


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")
    return ok


def main():
    tmp = tempfile.mkdtemp()
    data = os.path.join(tmp, "data")
    make_demo_data(data)
    md = B.load_data(data)
    cfg = B.base_cfg(B.load_half_spreads(data))
    T = len(md["close"])
    results = []
    params = {"breakout": dict(lookback=48, stop_atr=3.0, max_positions=4),
              "rotation": dict(lookback=168, rebalance_hours=24, max_positions=4)}

    base = cfg
    for profile, name in [(pr, n) for pr in ["standard", "aggressive", "aggressive_plus"] for n in params]:
        cfg = B.profile_cfg(base, profile)
        p = dict(params[name], max_positions=4 if profile == "standard" else 2)
        tag = f"{profile} {name}"
        eq_full, tr_full = B.run(md, STRATEGIES[name](**p), cfg)
        results.append(check(f"[{tag}] makes trades", len(tr_full) > 10, f"({len(tr_full)} trades)"))
        if cfg["max_adds"]:
            n_adds = int(tr_full["adds"].sum())
            results.append(check(f"[{tag}] adds to winners, never beyond limit",
                                 n_adds > 0 and tr_full["adds"].max() <= cfg["max_adds"],
                                 f"({n_adds} adds)"))
            added = tr_full[tr_full["adds"] > 0]
            results.append(check(f"[{tag}] adds only happen on winners (avg return of added trades > others)",
                                 added["return_pct"].mean() > tr_full[tr_full["adds"] == 0]["return_pct"].mean()))

        # 1. look-ahead: truncate the data and compare the shared history
        cut = int(T * 0.6)
        md_cut = {k: v.iloc[:cut] for k, v in md.items()}
        eq_cut, _ = B.run(md_cut, STRATEGIES[name](**p), cfg)
        same = np.allclose(eq_full.iloc[:cut - 1].to_numpy(), eq_cut.iloc[:cut - 1].to_numpy(),
                           equal_nan=True)
        results.append(check(f"[{tag}] no look-ahead (truncation test)", same))

        # 2. costs
        final = lambda c: B.run(md, STRATEGIES[name](**p), c)[0].iloc[-1]
        f0 = final(dict(cfg, fee=0.0, slippage=0.0, half_spread={}, default_half_spread=0.0))
        f1 = eq_full.iloc[-1]
        f2 = final(dict(cfg, fee=0.003, slippage=0.002))
        results.append(check(f"[{tag}] results fall as costs rise", f0 > f1 > f2,
                             f"(${f0:,.0f} > ${f1:,.0f} > ${f2:,.0f})"))

        # 3. never overspend / too many positions
        strat = STRATEGIES[name](**p)
        eq, tr = B.run(md, strat, cfg)
        if len(tr):
            over = (tr["cost_usd"] > cfg["capital"] * 20).any()  # sanity
            ev = pd.concat([pd.Series(1, tr["entry_time"]), pd.Series(-1, tr["exit_time"])]).sort_index()
            ev = ev.groupby(level=0).sum().cumsum()
            results.append(check(f"[{tag}] never more than {strat.max_positions} positions",
                                 ev.max() <= strat.max_positions, f"(max {ev.max()})"))
            results.append(check(f"[{tag}] equity always positive", (eq.dropna() > 0).all() and not over))

    # 4a. missing candles must not switch the market filter off for weeks
    gap_md = {k: v.copy() for k, v in md.items()}
    for k in ["open", "high", "low", "close"]:
        gap_md[k].iloc[3000:3003, gap_md[k].columns.get_loc("BTC")] = np.nan
    gap_md = B.fill_gaps(gap_md, verbose=False)
    from strategies import common_filters as _cf
    _, r0 = _cf(md, base)
    _, r1 = _cf(gap_md, base)
    results.append(check("3 missing BTC hours change the market filter for at most a few hours",
                         (r0 != r1).sum() <= 5, f"({(r0 != r1).sum()} hours differ)"))

    # 4. new features
    from strategies import common_filters
    _, reg_btc = common_filters(md, dict(base, breadth_min=0))
    _, reg_both = common_filters(md, dict(base, breadth_min=0.5))
    results.append(check("breadth filter only ever removes trading time",
                         (~reg_btc & reg_both).sum() == 0 and reg_both.sum() < reg_btc.sum(),
                         f"(BTC-only on {reg_btc.mean():.0%} of hours, with breadth {reg_both.mean():.0%})"))
    p = params["breakout"]
    _, t_loose = B.run(md, STRATEGIES["breakout"](**p), base)
    _, t_lock = B.run(md, STRATEGIES["breakout"](**p), dict(base, lock_gain=0.03, lock_atr=1.0))
    results.append(check("profit lock exits winners sooner",
                         t_lock["hours_held"].mean() < t_loose["hours_held"].mean(),
                         f"({t_lock['hours_held'].mean():.0f}h vs {t_loose['hours_held'].mean():.0f}h)"))
    s1 = STRATEGIES["breakout"](**p)
    s1.prepare(md, base)
    e_prep, _ = B.run(md, s1, base, 1000, 3000, prepared=True)
    e_norm, _ = B.run(md, STRATEGIES["breakout"](**p), base, 1000, 3000)
    results.append(check("re-using prepared indicators gives identical results", e_prep.equals(e_norm)))

    # 5. walk-forward folds never overlap and never peek
    import walk_forward as W
    folds = W.make_folds(md["close"].index, 3, 1)
    ok = all(b == c and a < b < d for a, b, c, d in folds) and \
        all(folds[k][3] == folds[k + 1][2] for k in range(len(folds) - 1))
    results.append(check("walk-forward: tuning always ends where trading starts; blocks don't overlap",
                         ok and len(folds) >= 3, f"({len(folds)} folds)"))
    import config as C
    old = C.WF_TRAIN_MONTHS, C.WF_TEST_MONTHS, C.PROFILES["standard"]["breakout_grid"]
    try:
        C.WF_TRAIN_MONTHS, C.WF_TEST_MONTHS = 3, 1
        W.main(data_dir=data, out_dir=os.path.join(tmp, "results_wf"))
        ok = os.path.exists(os.path.join(tmp, "results_wf", "summary.txt"))
    except Exception as e:
        print(e)
        ok = False
    finally:
        C.WF_TRAIN_MONTHS, C.WF_TEST_MONTHS = old[0], old[1]
    results.append(check("full walk-forward report runs", ok))

    # 6. end-to-end report
    try:
        B.main(data_dir=data, results_dir=os.path.join(tmp, "results"), make_chart=True)
        ok = os.path.exists(os.path.join(tmp, "results", "summary.txt"))
    except Exception as e:
        print(e)
        ok = False
    results.append(check("full backtest report runs", ok))

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL CHECKS PASSED" if all(results) else "\nSOME CHECKS FAILED")


if __name__ == "__main__":
    main()
