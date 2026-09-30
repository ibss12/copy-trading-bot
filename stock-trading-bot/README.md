# Stock Trading Bot (Lumibot + Alpaca paper trading)

A personal stock bot built on [Lumibot](https://github.com/Lumiwealth/lumibot). It watches a list of
stocks, trades a short/long **SMA crossover**, and can optionally **follow the big names** — hedge-fund
managers, Trump family filings, CEOs buying/selling their own stock, and members of Congress like
Nancy Pelosi — using their **public** disclosures.

> **Paper trading only.** `config.py` hard-codes `PAPER: True` for Alpaca and `run_live.py` refuses to
> start otherwise. No real money is ever traded.

This directory is standalone and does not use any of the Solana/crypto code in the rest of the repo.

## Layout

| File | What it does |
| --- | --- |
| `config.py` / `.env` | API keys, watchlist, position sizing, risk limits, who to follow |
| `strategies/sma_crossover.py` | `SmaCrossover` Lumibot strategy + risk controls |
| `strategies/smart_money.py` | `SmartMoneySma`: SMA crossover + "follow the big traders" signals |
| `signals/` | Public-disclosure data: SEC 13F, SEC Form 4, Quiver congressional trades |
| `run_backtest.py` | Backtest on Yahoo Finance history and print performance stats |
| `run_live.py` | Run live on your Alpaca **paper** account |
| `show_signals.py` | Just print what the people you follow recently bought/sold (no trading) |

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

## Disclaimer

For education and paper trading only. Past performance (including backtests and famous investors'
trades) does not predict future results.
