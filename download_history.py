"""
Long price history for the walk-forward test.

  python download_history.py

Revolut X only has about a year of candles. Binance publishes free hourly
candles going back years at data.binance.vision. For the big coins, Binance
prices move almost exactly like Revolut X prices, so the history is a good
stand-in. The backtest still charges Revolut X fees and Revolut X spreads.

- Uses the same coin list as data/spreads.csv (so run download_data.py first)
- Saves data_long/<COIN>.csv in the same format as the Revolut files
- Copies data/spreads.csv across so the Revolut spreads are used for costs
- Running it again only fetches months you don't have yet
Takes about 5-15 minutes the first time.
"""
import datetime as dt
import io
import os
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

import config as C

BASE = "https://data.binance.vision/data/spot"
COLS = ["start", "open", "high", "low", "close", "volume"]


def _fetch_zip(url, session):
    for attempt in range(4):
        try:
            r = session.get(url, timeout=60)
        except requests.RequestException:
            continue
        if r.status_code == 404:
            return None                      # coin didn't exist yet / file not published
        if r.ok:
            return r.content
    raise RuntimeError(f"could not download {url}")


def _parse(content):
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        raw = z.read(z.namelist()[0]).decode()
    df = pd.read_csv(io.StringIO(raw), header=None, usecols=range(6))
    if not str(df.iloc[0, 0]).strip().isdigit():   # some files have a header row
        df = df.iloc[1:]
    df.columns = COLS
    df = df.astype(float)
    # Binance switched spot files to MICROsecond timestamps from 2025; convert to ms
    df["start"] = df["start"].where(df["start"] < 1e14, df["start"] / 1000).astype("int64")
    return df


def months_between(first, last):
    y, m = first
    while (y, m) <= last:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def download_coin(coin, session):
    sym = C.BINANCE_NAMES.get(coin, coin) + "USDT"
    path = os.path.join(C.LONG_DATA_DIR, f"{coin}.csv")
    old = pd.read_csv(path) if os.path.exists(path) else None
    today = dt.datetime.now(dt.timezone.utc).date()
    last_full = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    if old is not None and len(old):
        last = pd.to_datetime(old["start"].max(), unit="ms", utc=True)
        first = (last.year, last.month)          # re-fetch the last month we have
    else:
        y, m = map(int, C.LONG_HISTORY_START.split("-"))
        first = (y, m)
    parts = []
    for y, m in months_between(first, last_full):
        url = f"{BASE}/monthly/klines/{sym}/1h/{sym}-1h-{y}-{m:02d}.zip"
        content = _fetch_zip(url, session)
        if content:
            parts.append(_parse(content))
    # this month so far, from the daily files
    d = today.replace(day=1)
    while d < today:
        url = f"{BASE}/daily/klines/{sym}/1h/{sym}-1h-{d:%Y-%m-%d}.zip"
        content = _fetch_zip(url, session)
        if content:
            parts.append(_parse(content))
        d += dt.timedelta(days=1)
    if not parts and old is None:
        return coin, 0, "not on Binance"
    df = pd.concat(([old] if old is not None else []) + parts)
    df = df.drop_duplicates("start").sort_values("start")
    df.to_csv(path, index=False)
    first_day = pd.to_datetime(df["start"].min(), unit="ms", utc=True)
    return coin, len(df), f"from {first_day:%Y-%m-%d}"


def main():
    spreads = os.path.join(C.DATA_DIR, "spreads.csv")
    if not os.path.exists(spreads):
        raise SystemExit("Run python download_data.py first (it picks the coin list).")
    os.makedirs(C.LONG_DATA_DIR, exist_ok=True)
    shutil.copy(spreads, os.path.join(C.LONG_DATA_DIR, "spreads.csv"))
    coins = list(pd.read_csv(spreads)["coin"])
    if "BTC" not in coins:
        coins.append("BTC")
    print(f"Downloading Binance hourly history for {len(coins)} coins (from {C.LONG_HISTORY_START})...")
    session = requests.Session()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for coin, n, note in pool.map(lambda c: _safe(c, session), coins):
            print(f"  {coin:8s} {n:7d} candles  {note}")
    print(f"\nDone. Files are in '{C.LONG_DATA_DIR}'. Next: python walk_forward.py")


def _safe(coin, session):
    try:
        return download_coin(coin, session)
    except Exception as e:
        return coin, 0, f"FAILED ({e})"


if __name__ == "__main__":
    main()
