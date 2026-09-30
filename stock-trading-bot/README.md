# Stock Trading Bot (Lumibot + Alpaca paper / Trading 212 practice)

A personal stock bot built on [Lumibot](https://github.com/Lumiwealth/lumibot). It watches a list of
stocks, trades a short/long **SMA crossover**, and can optionally **follow the big names** — hedge-fund
managers, Trump family filings, CEOs buying/selling their own stock, and members of Congress like
Nancy Pelosi — using their **public** disclosures.

> **Paper/practice trading only.** `config.py` hard-codes `PAPER: True` for Alpaca, and the Trading 212
> client refuses any server except `https://demo.trading212.com` (the practice account). No real money
> is ever traded.

This directory is standalone and does not use any of the Solana/crypto code in the rest of the repo.

## Layout

| File | What it does |
| --- | --- |
| `config.py` / `.env` | API keys, watchlist, position sizing, risk limits, who to follow |
| `strategies/sma_crossover.py` | `SmaCrossover` Lumibot strategy + risk controls |
| `strategies/smart_money.py` | `SmartMoneySma`: SMA crossover + "follow the big traders" signals |
| `signals/` | Public-disclosure data: SEC 13F, SEC Form 4, Quiver congressional trades |
| `run_backtest.py` | Backtest on Yahoo Finance history and print performance stats |
| `run_live.py` | Run live on your Alpaca **paper** or Trading 212 **practice** account |
| `trading212/` | Trading 212 practice API client, Lumibot broker, and the ledger of shares the bot bought |
| `tests/` | Trading 212 tests against a local fake server (`python -m unittest discover -s tests -t .`) |
| `show_signals.py` | Just print what the people you follow recently bought/sold (no trading) |
| `run_dashboard.py` / `dashboard/` | Browser **command center**: live prices, charts, alerts + pop-ups, paper account, bot controls |

## 1. Setup

Requires **Python 3.11+** (Lumibot's dependencies don't resolve cleanly on 3.10).

```bash
cd stock-trading-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env
```

`.env` is git-ignored — never commit it.

## 2. Create an Alpaca paper account

1. Sign up for free at <https://app.alpaca.markets/signup> (an email is enough; no funding needed for paper).
2. Log in and make sure the account switcher (top left) says **Paper Trading**.
3. On the paper dashboard, find **API Keys** on the right side and click **Generate New Keys**.
4. Copy the **Key** and **Secret** into `.env`:
   ```
   ALPACA_API_KEY=PK...
   ALPACA_API_SECRET=...
   ```
   Paper keys start with `PK`. The secret is only shown once — regenerate if you lose it.
5. You can reset the paper account balance any time from the Alpaca dashboard.

## 3. Configure

Everything lives in `.env` (see `.env.example` for all options):

- `WATCHLIST` — tickers to trade, e.g. `AAPL,TSLA,MSFT,NVDA,AMZN,GOOGL,META`
- `STRATEGY` — `smart_money` (SMA + big-trader signals) or `sma` (plain crossover)
- `SMA_SHORT_WINDOW` / `SMA_LONG_WINDOW` — e.g. 20 / 50 days
- `LIVE_SLEEPTIME` — how often the live bot checks, e.g. `30M`, `1H`, `1D`

### Risk controls

- `MAX_POSITION_PCT` — max fraction of the portfolio in any one stock (default `0.10` = 10%)
- `MAX_OPEN_POSITIONS` — max number of stocks held at once
- `MAX_DAILY_LOSS_PCT` — if the portfolio drops this much from the previous close, the bot stops buying
  for the rest of the session; a loss seen at the close blocks buying for the next session (set `0` to
  disable). Checked every iteration and in `after_market_closes`, so it also works in daily backtests.
- `LIQUIDATE_ON_DAILY_LOSS=true` — additionally sell everything when the daily loss limit is hit
- `PRICE_ALERT_PCTS` — log an alert when a stock moves this much in a day in percent (default `5,10`)

### Following the big traders (`STRATEGY=smart_money`)

All data is **public** and **delayed** — people legally have days to months to report trades. This is
not non-public insider information; it tells you what they already did, after the fact.

| Who | Source | Delay | Config |
| --- | --- | --- | --- |
| Hedge funds / big investors (Buffett, Aschenbrenner, Ackman) | SEC 13F quarterly holdings (free) | up to ~45 days after quarter end | `FOLLOW_FUNDS` |
| Named insiders (Donald J. Trump, Donald Trump Jr.) | SEC Form 4 (free) | 2 business days | `FOLLOW_INSIDERS` |
| CEOs of stocks on your watchlist buying/selling their own shares | SEC Form 4 (free) | 2 business days | `TRACK_CEO_TRADES`, `CEO_TITLES` |
| Nancy Pelosi and other members of Congress | STOCK Act disclosures via [Quiver Quantitative](https://www.quiverquant.com/) (API key required) | up to 45 days | `QUIVER_API_KEY`, `FOLLOW_POLITICIANS` |

How it trades:

- Each signal counts +1 (bought / added / new position) or −1 (sold / cut / exited) for that ticker, for
  `SIGNAL_LOOKBACK_DAYS` after it was **disclosed**.
- **Net sellers** on a stock you hold → the bot sells it.
- **Net buyers** → the bot buys it, as long as the SMA trend agrees (`REQUIRE_SMA_CONFIRMATION=true`).
- With `FOLLOW_EXPANDS_WATCHLIST=true`, stocks the big names are buying get added to your watchlist.
- Otherwise the normal SMA crossover rules apply.

Adding people: SEC sources take `Label:CIK` pairs. Look up a CIK at
<https://www.sec.gov/cgi-bin/browse-edgar?company=&CIK=&type=13F&action=getcompany> (search the fund or
person's name). For example, to follow Michael Burry's Scion:
`FOLLOW_FUNDS=...,Michael Burry (Scion):1649339`.

Notes:
- 13F filings only show quarterly snapshots, so "bought"/"sold" means the position changed between two
  quarters, not an exact trade date.
- Trump's Form 4 filings (mostly DJT) are usually gifts/other transfers, which are ignored — only real
  open-market buys (`P`) and sells (`S`) count.
- CEO sales made under a pre-scheduled 10b5-1 plan are ignored by default (`IGNORE_PLANNED_SALES=true`)
  since they don't signal anything.
- Set `SEC_USER_AGENT` to your name + email (SEC requires it). Responses are cached in `.cache/`.

See what the people you follow did recently, without trading:

```bash
python show_signals.py --days 60
```

## 4. Backtest (run this first)

```bash
python run_backtest.py                                  # uses BACKTEST_START/END/BUDGET from .env
python run_backtest.py --strategy sma --start 2024-01-01 --end 2025-06-30
python run_backtest.py --tearsheet                      # also save/open the HTML tearsheet + charts
```

Uses Lumibot's `YahooDataBacktesting` (free, no keys needed) and prints total return, CAGR, volatility,
Sharpe, and max drawdown vs. SPY. Trade logs and stats are written to `logs/`.

In `smart_money` backtests a signal is only used once its public **disclosure date** has passed, so there
is no look-ahead. 13F data goes back years (all quarters since the backtest start); Form 4 data is fetched for the last
~150 filings per person.

## 5. Run live (paper)

```bash
python run_live.py
```

This connects Lumibot's `Alpaca` broker (paper) to a `Trader`, runs during market hours at
`LIVE_SLEEPTIME`, and places market orders in your paper account. Watch orders/positions in the Alpaca
paper dashboard. Stop with `Ctrl+C`.

## 5b. Run live on a Trading 212 practice account

The bot can place its own entries and exits on your **Trading 212 practice (demo)** account, notify you
of each one, and still let you trade yourself.

1. In the Trading 212 app switch to your **Practice** account (Invest or Stocks ISA), then
   Settings &rarr; **API (Beta)** &rarr; generate a key. Copy the key and secret.
2. In `.env`: `BROKER=trading212`, `TRADING212_API_KEY=...`, `TRADING212_API_SECRET=...`.
   Leave `TRADING212_BASE_URL` at `https://demo.trading212.com/api/v0`; any other server is refused.
3. `python run_live.py` (or **Start bot** in the command center).

How it works:

- **Same strategy and risk limits** as on Alpaca (SMA / smart money, 10% per stock, max positions,
  daily-loss limit, skip a stock that already has an open order). Prices and charts still come from
  Yahoo, because Trading 212's API has no live price feed.
- **Your trades vs. the bot's:** Trading 212 only reports one combined position per stock, so the bot
  records every order it places in `.cache/trading212/bot_ledger.json` and counts its own filled
  shares. The strategy only sees those shares, and a bot sell is capped at them, so it never sells
  shares you bought yourself (in the app or from the dashboard). If you sell more than your own shares
  of a stock, the bot's count shrinks to what's left. Don't delete the ledger while the bot holds
  positions, or it will treat those shares as yours.
- **No double orders:** Trading 212 warns that repeating an order request can create two orders, so the
  bot never retries one. If a request gets no clear answer, the order is marked "unconfirmed" (blocking
  another order for that stock) until it shows up in Trading 212 or 10 minutes pass.
- **Notifications:** the command center pops up when the bot sends an order and when it fills
  ("Bot's order filled"), and when your own orders fill ("Your order filled").
- Only US stocks with market orders are supported. Balances show in your account's currency.

## 6. Command center (browser app)

```bash
python run_dashboard.py            # opens http://localhost:8000
python run_dashboard.py --no-browser --port 8080
```

A live dashboard for everything above. It runs on your machine only (listens on `127.0.0.1`).

- **Live prices:** watchlist + SPY/QQQ/DIA stream tick-by-tick from Yahoo Finance's websocket (with a
  60-second REST fallback), including pre-market and after-hours. The "Live" dot in the top bar shows
  the connection. Add/remove tickers right in the watchlist.
- **Charts:** click any ticker. 1D/5D show minute bars that update live; 1M–2Y show daily closes with
  the bot's 20/50-day averages, so you can see where it would buy (blue crosses above orange) or sell.
- **Alerts and pop-ups:** click **Turn on pop-ups** once to allow browser notifications. You get an
  in-page alert, a desktop pop-up and a sound for:
  - daily moves past your levels (default 3/5/10%) and sudden moves (default 1.5% in 5 minutes)
  - your own price alerts ("tell me when NVDA is below $200")
  - buy/sell signals (20/50-day average crossovers)
  - new filings from the big traders you follow (13F, Form 4, Congress with a Quiver key)
  - **buy/sell calls:** each watchlist stock gets a BUY / SELL / HOLD / WATCH badge with the reasons
    (trend, recent crossover, which big traders bought or sold). You get a pop-up when a stock turns
    BUY or SELL, plus a "Today's calls" summary when the dashboard starts
  - bot buys/sells, the daily-loss limit, and fills in your account (bot's and yours)

  Change levels in the gear (settings) menu. Settings are saved to `.cache/dashboard_settings.json`.
- **Trading 212 practice account** (`BROKER=trading212`): positions show how many shares are the bot's
  and how many are yours, and orders are labelled Bot/You. Buy/Sell on the dashboard places *your own*
  trade (the bot won't count those shares); it refuses a second order while one is open for that stock.
- **Paper account:** with Alpaca paper keys in `.env` it shows equity, today's P/L, buying power,
  positions, open orders and recent fills (refreshed every few seconds). You can place paper market
  orders, cancel orders, and close positions from the dashboard.
- **Bot controls:** start/stop `run_live.py` (SMA only, or smart money + SMA) and watch its log live.
- **Full-page view:** every panel has an expand button (or double-click its title) that opens it full
  page with more detail. The chart gets 1-week to 2-year performance, the 52-week range, what the bot
  sees (averages and last crossover), big-trader filings, price alerts and recent alerts for that stock.
  The watchlist adds open/high/low/volume and average columns. The pop-out button opens a panel in its
  own window (e.g. chart on a second monitor), and `http://localhost:8000/#full=chart` links straight
  to it. Press Esc to go back.

Without Alpaca keys, live prices, charts, big-trader moves and alerts still work; account and bot
sections show setup steps instead.

Keep the tab open (it can be in the background) to receive pop-ups. Yahoo data is for information only
and can occasionally lag; your paper orders fill at Alpaca's prices.

## Disclaimer

For education and paper trading only. Past performance (including backtests and famous investors'
trades) does not predict future results.
