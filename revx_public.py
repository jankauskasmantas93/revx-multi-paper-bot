"""
Small client for Revolut X PUBLIC market data (no API key needed).
Docs: https://developer.revolut.com/docs/api/revolut-x-crypto-exchange
Public endpoints allow about 1 request per second, so we wait between calls.
"""
import time
import requests

BASE = "https://revx.revolut.com/api"
_last_call = [0.0]


def _get(path, params=None, tries=5):
    for attempt in range(tries):
        wait = 1.1 - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            r = requests.get(BASE + path, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"  network error ({e}); retrying...")
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:  # rate limited: Retry-After is in milliseconds
            ms = float(r.headers.get("Retry-After", "2000"))
            time.sleep(ms / 1000 + 0.5)
            continue
        if r.status_code >= 500:
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Gave up calling {path}")


def pairs(region):
    """{'BTC/USD': {'base': 'BTC', 'quote': 'USD', 'status': 'active', ...}, ...}"""
    return _get("/1.0/public/configuration/pairs", {"region": region})


def tickers(region):
    """List of dicts with symbol, bid, ask, last_price, quote_volume_24h ..."""
    return _get("/1.0/public/tickers", {"region": region})["data"]


def candles(symbol_dash, interval_min, since_ms, until_ms, region):
    """symbol_dash like 'BTC-USD'. Returns list of dicts (start, open, high, low, close, volume)."""
    data = _get(f"/1.0/public/candles/{symbol_dash}",
                {"interval": interval_min, "since": int(since_ms),
                 "until": int(until_ms), "region": region})
    return data.get("data", [])
