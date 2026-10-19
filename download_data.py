"""
Step 1: download data from Revolut X.

  python download_data.py

- Picks the NUM_COINS most-traded USD coins on Revolut X (stablecoins skipped)
- Saves each coin's bid/ask spread right now to data/spreads.csv
- Downloads 1-hour candles for each coin to data/<COIN>.csv
Running it again only downloads the new candles since last time.
Takes roughly 10-20 minutes the first time (Revolut allows 1 request/second).
"""
import os
import time
import pandas as pd

import config as C
import revx_public as api


def pick_coins():
    active = {s for s, p in api.pairs(C.REGION).items()
              if p.get("quote") == C.QUOTE and p.get("status") == "active"}
    rows = []
    for t in api.tickers(C.REGION):
        sym = t["symbol"]
        base, quote = sym.split("/")
        if quote != C.QUOTE or base in C.EXCLUDE or sym not in active:
            continue
        bid, ask = float(t.get("bid") or 0), float(t.get("ask") or 0)
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2
        rows.append({"coin": base, "symbol": sym,
                     "quote_volume_24h": float(t.get("quote_volume_24h") or 0),
                     "bid": bid, "ask": ask,
                     "spread_pct": (ask - bid) / mid * 100})
    df = pd.DataFrame(rows).sort_values("quote_volume_24h", ascending=False)
    return df.head(C.NUM_COINS).reset_index(drop=True)


def download_coin(coin, now_ms, data_dir=None):
    path = os.path.join(data_dir or C.DATA_DIR, f"{coin}.csv")
    step = C.CANDLE_MINUTES * 60_000
    old = None
    if os.path.exists(path):
        old = pd.read_csv(path)
        start = int(old["start"].max()) + step
    else:
        start = now_ms - C.HISTORY_DAYS * 86_400_000
    start -= start % step
    rows, cursor, empty_run = [], start, 0
    while cursor < now_ms:
        end = min(cursor + 1000 * step, now_ms)
        got = api.candles(f"{coin}-{C.QUOTE}", C.CANDLE_MINUTES, cursor, end, C.REGION)
        if got:
            rows.extend(got)
            last = max(int(r["start"]) for r in got)
            # if the API returned fewer than asked, continue after the last candle
            cursor = max(last + step, cursor + step) if last + step < end else end
            empty_run = 0
        else:
            cursor = end
            empty_run += 1
    new = pd.DataFrame(rows)
    if not new.empty:
        new = new[["start", "open", "high", "low", "close", "volume"]].astype(float)
        new["start"] = new["start"].astype("int64")
    df = pd.concat([old, new]) if old is not None else new
    if df is None or df.empty:
        return 0
    df = df.drop_duplicates("start").sort_values("start")
    # drop the still-forming last candle
    df = df[df["start"] + step <= now_ms]
    df.to_csv(path, index=False)
    return len(df)


def main():
    os.makedirs(C.DATA_DIR, exist_ok=True)
    print("Finding the most-traded coins on Revolut X...")
    coins = pick_coins()
    coins.to_csv(os.path.join(C.DATA_DIR, "spreads.csv"), index=False)
    print(coins[["coin", "quote_volume_24h", "spread_pct"]].to_string(index=False))
    if "BTC" not in set(coins["coin"]):
        coins = pd.concat([coins, pd.DataFrame([{"coin": "BTC"}])])  # needed for the market filter
    now_ms = int(time.time() * 1000)
    for i, coin in enumerate(coins["coin"], 1):
        print(f"[{i}/{len(coins)}] {coin} ...", end=" ", flush=True)
        try:
            n = download_coin(coin, now_ms)
            print(f"{n} candles")
        except Exception as e:
            print(f"FAILED ({e})")
    print(f"\nDone. Files are in the '{C.DATA_DIR}' folder. Next: python backtest.py")


if __name__ == "__main__":
    main()
