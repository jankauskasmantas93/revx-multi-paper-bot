# Revolut X multi-coin bot: testing, paper trading and dashboard

## Run it in the cloud, laptop off (recommended)

GitHub (free) runs the paper trader every hour on its own computers and publishes your
dashboard as a web page you can open on your laptop or phone:
**https://YOUR-GITHUB-NAME.github.io/revx-paper-bot/**

One-time setup, about 10 minutes, all in the web browser:

1. **Create a free GitHub account** at https://github.com/signup.
2. **Create a repository:** go to https://github.com/new. Name it `revx-paper-bot`, choose
   **Public** (free hosting for the dashboard needs Public; no passwords or keys are
   stored in it), leave everything else unticked, and click **Create repository**.
3. **Upload the bot:** on the new, empty repository page, click the link
   **"uploading an existing file"**. Open this `revx-bot` folder in File Explorer, select
   everything **except** the `data`, `data_long`, `results`, `paper` and `site` folders
   (make sure the **`.github`** folder *is* included), and drag it all into the browser.
   Wait for the uploads to finish, then click **Commit changes**.
4. **Turn on the dashboard:** go to **Settings → Pages**. Under *Build and deployment →
   Source*, choose **GitHub Actions**.
   *If the Actions tab later shows no "Paper bot (hourly)" workflow, the `.github` folder
   didn't upload. Fix it: click **Add file → Create new file**, type the name
   `.github/workflows/paper-bot.yml`, paste in the contents of that file from this folder
   (open it with Notepad), and click **Commit changes**.*
5. **Start the bot:** go to the **Actions** tab. If GitHub asks, click the green button to
   enable workflows. Click **Paper bot (hourly)** on the left, then **Run workflow →
   Run workflow**. The first run takes about 15 minutes, because it downloads a year of
   prices. After that, each hourly run takes 1-2 minutes.
6. When the run shows a green tick, open **https://YOUR-GITHUB-NAME.github.io/revx-paper-bot/**
   and bookmark it, on your phone too.
7. **Stop the paper trader on your laptop** (Ctrl+C in its window). The cloud one replaces it.

From then on it runs by itself at about 5 past every hour. GitHub sometimes starts a run
a few minutes late; the bot catches up on any missed hours. Each run saves the paper
account into the repository (the `paper` folder), so nothing is lost between runs.

If a run fails (a red cross in the Actions tab), click it, copy the error, and send it to me.

## The dashboard

It shows:
- the paper account value (in $ and £), the return compared with just holding BTC, and the
  worst drop
- the market filter: whether the bot is allowed to buy right now
- a candlestick chart for any coin (1-hour, 4-hour and daily). It has the buy trigger line,
  your average buy price, your stop, and the bot's buys and sells marked on it.
  Scroll to zoom, drag to move, double-click to reset.
- open positions, with their value, profit or loss, and stop
- the coins **closest to a buy**
- the paper account against holding BTC over time
- a watchlist of every coin: price, 24-hour and 7-day change, a 7-day mini chart, its buy
  trigger and how far away it is. Click a coin to chart it; click a column to sort.
- every order and closed trade

The page is rebuilt every hour by the bot. When your browser is allowed to fetch Revolut X
prices directly, it also updates prices every minute, and a blue **Live prices** badge shows.
If that badge isn't there, the prices are from the last hourly run.

You can also build it on your laptop: `python dashboard.py` makes `site/index.html` and opens it.

This folder tests strategies on about 40 Revolut X coins, using Revolut X's real
costs, and paper-trades the winner (Slow trend) on live prices with fake money.
**Nothing here places real orders or needs an API key.**

## Paper trading on your laptop (alternative to the cloud)

Start it by double-clicking **`start_paper_trading.bat`** (Windows) or
**`start_paper_trading.command`** (Mac). Or run it from a terminal:

```
python paper_trader.py
```

Leave the window open. Just after every hour it downloads the newest candles, applies
stops, decides what to buy or sell, and "fills" the orders at the live Revolut X price,
including the real spread, the fee and slippage. The window shows each action.

To see how it's doing at any time, open a second terminal and run:
```
python status.py
```
This shows the paper pot, open positions and their stops, closed trades, how holding
BTC did over the same time, and **what the backtest says should have happened over
the same hours**. The two should stay close. If status.py warns that they differ,
send me its output.

Good to know:
- **It starts with $135 of fake money (about £100)** and uses the settings the
  walk-forward test chose most recently (from `results_wf/folds.csv`). So run
  `walk_forward.py` once before starting it.
- **Slow trend trades rarely, about once a week.** Several days with no trades is
  normal. It is waiting for a coin to break to a new multi-week high while BTC is in
  an uptrend.
- **Sleep and restarts are fine.** If the laptop sleeps or you close the window, just
  start it again. It catches up on the missed hours, applying stops to the missed
  candles the way a stop order resting on the exchange would, and carries on.
  For the most realistic test, set the laptop not to sleep while plugged in.
- **To stop it:** press Ctrl+C in its window.
- **To start again from zero:** `python paper_trader.py --reset`
- **Everything is saved in the `paper` folder:** `state.json` (the account),
  `orders.csv`, `trades.csv`, `equity.csv`, and `paper.log` (everything it did).
- **Safety checks:** it won't trade if the newest candle hasn't arrived yet, and it
  skips an order if the live price is more than 5% away from the last close, which
  would suggest bad data.
- **It runs exactly the same trading code as the backtest** (`book.py`). A test
  (`python test_paper.py`) replays old candles through the paper trader, restarting it
  every hour. It must end up with exactly the same trades and equity as the backtest.

| File | What it does |
|---|---|
| `config.py` | Every setting (coins, capital, fees, filters, profiles, settings to try). Edit only this file. |
| `download_data.py` | Picks the coins and downloads 1-hour candles and current spreads from Revolut X (about 1 year) |
| `download_history.py` | Downloads long 1-hour history (2020 onwards) for the same coins from Binance's free data site |
| `walk_forward.py` | **The main test:** rolling tune-then-trade over years of long history |
| `backtest.py` | Quick single TRAIN/TEST split on the Revolut X year |
| `strategies.py` | The two strategies: **breakout** and **rotation** |
| `paper_trader.py` | **Paper trading** on live Revolut X prices with fake money |
| `status.py` | How the paper account is doing, compared with the backtest |
| `dashboard.py` | Builds the dashboard web page (`dashboard_template.html` is its design) |
| `.github/workflows/paper-bot.yml` | The hourly cloud run (GitHub Actions) |
| `book.py` | The trading rules (sizing, stops, orders), shared by the backtest and the paper bot |
| `self_test.py` | Checks the backtesting is honest (45 checks, no internet needed) |
| `test_paper.py` | Checks the paper trader matches the backtest exactly (no internet needed) |
| `revx_public.py` | Tiny client for Revolut X public market data |

## One-time setup

1. Unzip this folder somewhere of its own, e.g. `Documents\revx-multi-bot`. If you
   already have the `data` folder from before, copy it in to skip a re-download.
2. Open a terminal in that folder:
   - **Windows:** open the folder in File Explorer, click the address bar, type `cmd` and press Enter.
   - **Mac:** right-click the folder and choose *New Terminal at Folder*.
3. Install the libraries (one time only):
   ```
   python -m pip install -r requirements.txt
   ```
   On a Mac, use `python3` instead of `python` in every command.

## Run the long walk-forward test

```
python self_test.py          (should end with ALL CHECKS PASSED)
python download_data.py      (picks the coins; only fetches new candles if you ran it before)
python download_history.py   (5-15 min the first time)
python walk_forward.py       (roughly 10-30 min)
```

Then open the `results_wf` folder:
- `summary.txt`: the main result, returns by calendar year, and the robustness table.
- `equity.png`: out-of-sample equity and drawdown against holding BTC and an equal mix.
- `folds.csv`: every 3-month block, the settings chosen for it, and how it did against BTC.
- `all_settings.csv`: every setting held fixed for the whole period.
- `trades_*.csv`: every trade.

### How the walk-forward test works

It tunes the settings on 12 months, then trades the next 3 months with them. Then
it moves forward 3 months and repeats, all the way to today. Only the traded months,
which the settings never saw, count towards the result. It's the closest thing to
running the bot for real over several years without risking money.

## The strategies

**Breakout (the lead candidate).** It buys a coin when the coin closes above its
highest price of the last 12-96 hours, but only while the market is in an uptrend.
When several coins break out together, it buys the strongest first. It sells on an
ATR trailing stop, or when the coin closes below its recent low.

**Rotation.** It regularly ranks coins by momentum and holds the top few. In the first
test it traded too often and costs ate it, so the walk-forward test skips it by
default. To include it, add `"rotation"` to `WF_STRATEGIES` in `config.py`.

## Profiles

| | Standard | Aggressive | Aggressive plus |
|---|---|---|---|
| Coins held at once | 3-5 | 2-3 | 2-3 |
| Position size | equal slices | risk-based (a stop-out loses ~3% of the pot) | same as Aggressive |
| Max in one coin | 35% of pot | 60% of pot | 60% of pot |
| Adding to winners | never | up to 2 adds, each after a further +2 ATR | same as Aggressive |
| Market filter | BTC above its 50-day average | same | BTC **and** at least half of all coins above their 50-day averages |
| Profit lock | no | no | once a position is up 15%, its trailing stop tightens to 2 ATR |

**Slow trend** uses the same breakout logic on a days-to-weeks scale: it buys a
10, 20 or 40-day high and uses a wide trailing stop, so it makes far fewer trades.

Aggressive plus adds the two fixes suggested by the first Revolut X test: no buying
back in too early after a crash, and not giving big gains back.

## Safeguards built in (the live bot will use the same ones)

- Every fill is charged the 0.09% taker fee, 0.05% slippage and half of that coin's
  measured Revolut X bid/ask spread.
- It skips coins with a spread above 0.4% or less than $20k traded in 24 hours.
- After a 5% loss in one day, it opens no new trades until the next day.
- Every position has a trailing stop, and there's a cap on how much goes into one coin.
- Signals only use closed candles. Orders fill at the next candle's open.
- Missing candles (exchange maintenance) are filled as flat candles, so a single
  gap can't switch the market filter off for weeks.

## Things to know (read before trusting any result)

- **Survivor bias.** The coin list is today's top 40 on Revolut X. Those are coins that
  survived and grew. Coins that collapsed and were delisted since 2020 aren't in the
  test. This makes every result look better than reality, especially "hold equal mix".
  Compare the strategies against each other and against BTC, and treat the absolute
  numbers as optimistic.
- Binance prices stand in for Revolut X prices before Revolut X's own history starts.
  For big coins they track closely. The costs are still Revolut X's.
- Binance volumes are much bigger than Revolut X's, so the volume filter hardly bites
  in the long test.
- The spreads come from one snapshot, not an average.
- A good backtest is not a promise. The next stages are paper trading on live
  prices for several weeks, then small real money.
