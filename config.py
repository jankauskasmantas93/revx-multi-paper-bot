"""
All settings live here. Change numbers in this file, not in the other files.
"""

# ---------------------------------------------------------------- data
REGION = "UK"               # Revolut X market data region: "UK" or "EEA"
QUOTE = "USD"               # we trade coins against USD
NUM_COINS = 40              # how many coins to track (picked by 24h volume)
HISTORY_DAYS = 730          # how much 1h history to try to download (2 years)
CANDLE_MINUTES = 60         # 1-hour candles
DATA_DIR = "data"           # where downloaded candles are saved
LONG_DATA_DIR = "data_long" # long Binance history (for walk_forward.py)
LONG_HISTORY_START = "2020-01"  # first month of Binance history to download
# Revolut X ticker -> Binance ticker, when they differ (add more if a coin is missing)
BINANCE_NAMES = {}
RESULTS_DIR = "results"     # where backtest reports are saved

# Coins never to trade (stablecoins and fiat-pegged tokens do not move)
EXCLUDE = {"USDT", "USDC", "DAI", "EURC", "EURT", "PYUSD", "FDUSD", "TUSD",
           "USDP", "GUSD", "USDE", "RLUSD", "USD1", "EUR", "GBP", "USD"}

# ---------------------------------------------------------------- money
START_CAPITAL_USD = 135.0   # roughly £100; every backtest starts here
MIN_ORDER_USD = 2.0         # skip orders smaller than this (Revolut X minimum is tiny)

# ---------------------------------------------------------------- costs
# Revolut X: maker 0%, taker 0.09%. We assume the worse case (taker)
# for every fill unless you change FEE_PER_SIDE to 0.0.
FEE_PER_SIDE = 0.0009
SLIPPAGE = 0.0005           # extra 0.05% per fill for price moving against us
DEFAULT_HALF_SPREAD = 0.001 # used when we have no spread measurement for a coin
MAX_SPREAD = 0.004          # never trade a coin whose bid/ask spread > 0.4%

# ---------------------------------------------------------------- filters
MIN_24H_VOLUME_USD = 20000  # coin must trade at least this much per 24h
BTC_REGIME_HOURS = 24 * 50  # only open new trades when BTC > its 50-day average
DAILY_LOSS_LIMIT = 0.05     # stop opening trades for the day after -5% on the day

# ---------------------------------------------------------------- testing
TRAIN_FRACTION = 0.65       # first 65% of history tunes settings, last 35% tests them
MIN_TRADES_TO_RANK = 20     # a setting needs at least this many trades to be chosen

# ---------------------------------------------------------------- profiles
# The backtest runs BOTH profiles and shows them side by side.
#
# Sizing:
#   risk_per_trade = None -> every position gets an equal slice (pot / max_positions)
#   risk_per_trade = 0.03 -> size each position so that hitting its stop loses ~3%
#                            of the pot. Calm coins get more money, wild coins less.
#   position_cap          -> never put more than this share of the pot in one coin
# Adding to winners (pyramiding):
#   max_adds      -> how many times it may add to a winning position (0 = never)
#   add_atr       -> add each time price rises this many ATRs above the last buy
#   add_fraction  -> each add is this fraction of the first buy
#   Adds only happen on winners and only while BTC is in an uptrend. The trailing
#   stop protects the whole, bigger position.
#
# grids -> the settings the optimiser tries on the TRAIN period only.
PROFILES = {
    "standard": {
        "risk_per_trade": None, "position_cap": 0.35,
        "max_adds": 0, "add_atr": 2.0, "add_fraction": 0.5,
        "breakout_grid": {
            "lookback": [24, 48, 96],        # hours for the breakout high
            "stop_atr": [2.0, 3.0, 4.0],     # trailing stop distance in ATRs
            "max_positions": [3, 5],
        },
        "rotation_grid": {
            "lookback": [72, 168, 336],      # hours of momentum to rank coins on
            "rebalance_hours": [24, 72],     # how often to re-rank
            "max_positions": [3, 5],
        },
    },
    "aggressive": {
        "risk_per_trade": 0.03, "position_cap": 0.60,
        "max_adds": 2, "add_atr": 2.0, "add_fraction": 0.5,
        "breakout_grid": {
            "lookback": [12, 24, 48],
            "stop_atr": [2.5, 3.5, 5.0],
            "max_positions": [2, 3],
        },
        "rotation_grid": {
            "lookback": [48, 72, 168],
            "rebalance_hours": [12, 24],
            "max_positions": [2, 3],
        },
    },
    # Aggressive + the two fixes suggested by the first Revolut X test:
    #   breadth_min -> only trade when at least this share of coins are above their
    #                  50-day average (not just BTC). Stops early re-entry in a weak market.
    #   lock_gain / lock_atr -> once a position is up lock_gain (15%) from its first
    #                  buy, tighten its trailing stop to lock_atr ATRs to keep the profit.
    "aggressive_plus": {
        "risk_per_trade": 0.03, "position_cap": 0.60,
        "max_adds": 2, "add_atr": 2.0, "add_fraction": 0.5,
        "breadth_min": 0.5, "lock_gain": 0.15, "lock_atr": 2.0,
        "breakout_grid": {
            "lookback": [12, 24, 48],
            "stop_atr": [2.5, 3.5, 5.0],
            "max_positions": [2, 3],
        },
        "rotation_grid": {
            "lookback": [48, 72, 168],
            "rebalance_hours": [12, 24],
            "max_positions": [2, 3],
        },
    },
    # Slow trend-following: same breakout logic but on a days-to-weeks scale.
    # Buys a 10/20/40-day high, wide trailing stop (hourly ATRs x 8-16, roughly
    # 8-25% below the high), few trades so costs matter far less.
    "slow_trend": {
        "risk_per_trade": None, "position_cap": 0.40,
        "max_adds": 0, "add_atr": 2.0, "add_fraction": 0.5,
        "min_trades": 8,                      # it trades rarely; 8 in a year is enough to rank
        "breakout_grid": {
            "lookback": [240, 480, 960],      # 10, 20, 40 days in hours
            "stop_atr": [8.0, 12.0, 16.0],
            "max_positions": [3, 5],
        },
        "rotation_grid": {
            "lookback": [336, 720],
            "rebalance_hours": [168],
            "max_positions": [3, 5],
        },
    },
}

# ---------------------------------------------------------------- walk-forward
# walk_forward.py: tune on WF_TRAIN_MONTHS, trade the next WF_TEST_MONTHS with
# those settings, move forward WF_TEST_MONTHS, repeat. Only the traded
# (never-tuned-on) months are stitched together into the result.
WF_TRAIN_MONTHS = 12
WF_TEST_MONTHS = 3
WF_STRATEGIES = ["breakout"]                 # add "rotation" to test it too
WF_PROFILES = ["standard", "aggressive", "aggressive_plus", "slow_trend"]

# The settings used when you run a single, un-optimised backtest
BREAKOUT_DEFAULT = {"lookback": 48, "stop_atr": 3.0, "max_positions": 4,
                    "exit_lookback": None, "atr_len": 24, "max_hold": None}
ROTATION_DEFAULT = {"lookback": 168, "rebalance_hours": 24, "max_positions": 4,
                    "buffer": 2, "stop_atr": 5.0, "atr_len": 24}

# ---------------------------------------------------------------- paper trading
PAPER_DIR = "paper"                 # state, logs, trades and equity files
PAPER_START_USD = 135.0             # fake starting pot (about £100)
PAPER_PROFILE = "slow_trend"
PAPER_STRATEGY = "breakout"
# Settings: "auto" takes the ones the walk-forward chose most recently
# (results_wf/folds.csv). If that file is missing, these are used instead.
PAPER_PARAMS = "auto"
PAPER_PARAMS_FALLBACK = {"lookback": 480, "stop_atr": 12.0, "max_positions": 5}
MAX_PRICE_JUMP = 0.05               # skip an order if the live price is >5% away from the last close (bad data guard)
CYCLE_DELAY_SECONDS = 90            # run each hourly cycle this long after the hour, so the candle is final
