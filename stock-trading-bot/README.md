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
| `signals/` | Public-disclosure data: SEC 13F, SEC Form 4, congressional trades (Bargo, or Quiver) |
| `run_backtest.py` | Backtest on Yahoo Finance history and print performance stats |
| `run_live.py` | Run live on your Alpaca **paper** or Trading 212 **practice** account |
| `trading212/` | Trading 212 practice API client, Lumibot broker, and the ledger of shares the bot bought |
| `tests/` | Trading 212 (local fake server), login, multi-account, push and bot-restart tests (`python -m unittest discover -s tests -t .`) |
| `show_signals.py` | Just print what the people you follow recently bought/sold (no trading) |
| `run_dashboard.py` / `dashboard/` | Browser **command center**: live prices, charts, alerts + pop-ups, paper account, bot controls |
| `deploy/oracle/install.sh` | One-command install on an Ubuntu cloud server (Oracle Always Free): systemd + Caddy HTTPS |
| `run_scheduled.py` / `scheduled/` / `notify/` | One bot check that trades the practice account and sends Discord messages, then exits (free GitHub schedule, section 8) |
| `../.github/workflows/stock-bot.yml` | Runs `run_scheduled.py` on GitHub every 15 minutes on US market days |

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
| Nancy Pelosi and other members of Congress | STOCK Act disclosures via [Bargo](https://www.bargo.ai/free-apis/congress) (free, last 3 months; no key needed, or a free `BARGO_API_KEY` for 10x the daily limit when following several politicians), or [Quiver Quantitative](https://www.quiverquant.com/) if `QUIVER_API_KEY` is set (paid) | up to 45 days | `FOLLOW_POLITICIANS`, `BARGO_API_KEY`, `QUIVER_API_KEY` |

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

A live dashboard for everything above. It runs on your machine only (listens on `127.0.0.1`); see
section 7 to run it on an always-on cloud server and open it from your phones.

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
  - new filings from the big traders you follow (13F, Form 4, Congress)
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

Keep the tab open (it can be in the background) to receive pop-ups, or turn on notifications on the
cloud install to get them with it closed. Yahoo data is for information only
and can occasionally lag; your paper orders fill at Alpaca's prices.

### Multiple practice accounts, login and phone notifications

- **Several Trading 212 practice accounts:** the account menu in the top bar switches between them and
  has **+ Add practice account…** (paste a practice API key and secret) and **Manage accounts…**. Each
  account has its own bot (start/stop separately), its own record of the bot's shares
  (`.cache/trading212/accounts/<id>/`), and its own balance, positions and orders. A bot can only ever
  trade, and sell, in its own account. The account from `.env` is always the first one. Every account is
  checked against Trading 212's practice server before it's saved; real-money keys don't work there.
  Added keys are saved in `.cache/trading212/accounts.json`, readable only by the user running the app.
- **Bots keep running:** a bot you started is remembered and starts again by itself after a restart of
  the command center or the server. If a bot that has been running for a while stops unexpectedly, it is
  restarted after a minute (and you get a notification); if it crashes straight away it is left off so
  you can look at its log. Restarts never resend an order that didn't get a clear answer.
- **Password:** set `DASHBOARD_PASSWORD` in `.env` to open the command center from other devices.
  Without one it only answers on this computer (`http://localhost`). You stay signed in for 90 days on
  each device; changing the password signs every device out. After 5 wrong passwords an address is
  locked out for 15 minutes. **Sign out** is in the gear menu.
- **Install it as an app:** on the `https://` address of your cloud install (section 7):
  - **iPhone/iPad:** open it in **Safari** &rarr; Share button &rarr; **Add to Home Screen**, then open
    it from the new icon. iPhones only allow notifications for apps added to the home screen (iOS 16.4+).
  - **Android:** open it in **Chrome** &rarr; &#8942; menu &rarr; **Install app** (or Add to Home screen).
- **Notifications when it's closed:** open the gear menu &rarr; *This device* &rarr; **Turn on** and allow
  notifications. Do this on each phone or computer. You then get buy/sell calls, big-trader filings, your
  price alerts, and bot orders, fills, cancellations and errors even with the app closed and the phone
  locked. **Send test** checks it works. With several accounts, tick which accounts' bot and order alerts
  this device gets (buy/sell calls and big-trader alerts go to every device). Notifications contain the
  alert text only, never your keys.

## 7. Run it 24/7 on a free Oracle Cloud server

This puts the command center and the bots on a small computer in Oracle's cloud that stays on all the
time, so trading and notifications continue with your phones and laptop off. Oracle's "Always Free"
tier costs nothing; sign-up asks for a card to check you're a real person.

**1. Create the account.** Go to https://www.oracle.com/cloud/free/ &rarr; *Start for free*. Choose a
home region close to you (it can't be changed later).

**2. Create the server.** In the Oracle Cloud console: &#9776; menu &rarr; *Compute* &rarr; *Instances*
&rarr; **Create instance**.
- *Image and shape* &rarr; *Edit*: image **Canonical Ubuntu 24.04**; shape **Ampere VM.Standard.A1.Flex**
  with 1 OCPU and 6 GB memory (both marked *Always Free-eligible*). If it says "out of capacity", try
  another *Availability domain*, or pick **VM.Standard.E2.1.Micro** instead (the install script adds
  swap space for its small memory).
- *Networking*: keep "Create new virtual cloud network" and "Create new public subnet", and make sure
  **Assign a public IPv4 address** is on.
- *Add SSH keys*: **Generate a key pair for me** &rarr; **Save private key** (keep this file safe).
- **Create**, wait until it shows *Running*, and note the **Public IP address**.

**3. Open the web ports.** On the instance page click the **subnet** link &rarr; *Security* (or
*Security Lists*) &rarr; the *Default Security List* &rarr; **Add Ingress Rules**: source CIDR
`0.0.0.0/0`, IP protocol TCP, destination port range `80,443` &rarr; **Add Ingress Rules**.

**4. Connect to the server.** From a computer's terminal (Terminal on Mac, PowerShell on Windows):

```bash
chmod 600 ~/Downloads/ssh-key-*.key        # Mac/Linux only
ssh -i ~/Downloads/ssh-key-*.key ubuntu@YOUR.PUBLIC.IP
```

(Or use the *Cloud Shell* button at the top of the Oracle console: upload the key from its menu, then
run the same `ssh` command.) Type `yes` the first time.

**5. Install.** On the server, run this, choosing your own long password:

```bash
curl -fsSL https://raw.githubusercontent.com/ibss12/copy-trading-bot/master/stock-trading-bot/deploy/oracle/install.sh \
  | sudo STOCKBOT_PASSWORD='choose-a-long-password' bash
```

It takes about 5–10 minutes and ends by printing your address, e.g. `https://132-145-1-2.sslip.io`
(a free name that points at your server's IP, so no domain is needed; HTTPS certificates are set up
automatically by Caddy). To use your own domain, point it at the IP and add
`STOCKBOT_DOMAIN=bot.example.com` before `bash`. (Before this PR is merged, add
`STOCKBOT_BRANCH=devin/1790776916-lumibot-stock-bot` and use that branch name instead of `master` in
the URL.)

**6. Use it.** Open the address on each phone, sign in, install it to the home screen and turn on
notifications (section 6 above), then **+ Add practice account…** and start the bot for each account.
The bot then trades your practice accounts around the clock, and the trades appear in the Trading 212
app as usual.

What the script sets up, and how to look after it:
- The app lives in `/opt/stockbot` and runs as a `stockbot` system user under systemd
  (`stockbot.service`, restarted automatically if it stops and started at boot). It listens only on
  `127.0.0.1:8000`; Caddy serves it to the internet over HTTPS on ports 80/443, and the server's
  firewall (iptables) is opened for just those two ports.
- Settings: `/opt/stockbot/app/stock-trading-bot/.env` (password, `BROKER=trading212`, `PUBLIC_URL`).
  Edit with `sudo nano` and apply with `sudo systemctl restart stockbot`. Other API keys (Quiver,
  `SEC_USER_AGENT`) go there too. Accounts, bot records, push keys and settings are in `.cache/` next to
  it and survive updates.
- **Update:** gear menu &rarr; **Update app** (pulls the latest code, installs packages, restarts), or
  run the install command again (without `STOCKBOT_PASSWORD` it keeps your password).
- **Logs:** `sudo journalctl -u stockbot -f`. Status: `systemctl status stockbot caddy`.
- If the page doesn't load: check the ingress rules from step 3, and that `curl -I http://localhost:8000/login`
  on the server answers.
- Oracle may reclaim Always Free servers that sit almost completely idle for a week; upgrading the
  account to *Pay As You Go* (still free for these resources) avoids that.

## 8. Free: GitHub + Discord (no server, no app)

GitHub runs the bot for you, for free (public repo), about every 15 minutes while the US market is
open. Each run checks your stocks, trades your Trading 212 **practice** account with the same rules
and limits as above, and sends what happened to a **Discord** channel, so your phones get notified
with everything closed. There's no live dashboard in this mode; you can still trade yourself in the
Trading 212 app and the bot only sells shares it bought.

You get messages for: bot buys and sells (with the reason), fills, cancelled or refused orders,
stocks turning BUY or SELL, new big-trader filings, big daily price moves (`PRICE_ALERT_PCTS`), the
daily loss limit, problems such as a wrong key (once a day), and a summary after the close.

**1. Make a Discord channel link (webhook).** In the Discord app (a computer or phone browser is
easiest): create a server for yourself (**+** &rarr; *Create My Own*) or use one you own. Next to a
text channel tap the gear (*Edit Channel*) &rarr; **Integrations** &rarr; **Webhooks** &rarr;
**New Webhook** &rarr; **Copy Webhook URL**. Keep this link private: anyone who has it can post in
the channel.

**2. Get your Trading 212 practice key** (Trading 212 app &rarr; switch to **Practice** &rarr;
Settings &rarr; API (Beta) &rarr; generate a key; copy the key and the secret). You can skip this at
first: without it the bot doesn't trade but still sends advice and big-trader alerts.

**3. Put them in GitHub's hidden settings.** Open
https://github.com/ibss12/copy-trading-bot/settings/secrets/actions &rarr; **New repository secret**,
once for each:

| Name | Value |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | the webhook link from step 1 |
| `TRADING212_API_KEY` | your practice API key |
| `TRADING212_API_SECRET` | your practice API secret |
| `BARGO_API_KEY` (optional) | free key from https://www.bargo.ai/free-apis/dash; lets the bot follow more politicians per day |
| `QUIVER_API_KEY` (optional) | paid Quiver key; without it Congress trades come free from Bargo |

Secrets are hidden, even though the repo is public. To change settings such as `WATCHLIST`,
`MAX_POSITION_PCT` or `STRATEGY`, use the **Variables** tab on the same page (same names as in
`.env.example`).

**4. Start it.** Open https://github.com/ibss12/copy-trading-bot/actions &rarr; if asked, click
*I understand my workflows, go ahead and enable them* &rarr; **Stock bot (practice + Discord)** &rarr;
**Run workflow**. Within a few minutes Discord should show "Stock bot is connected". After that it
runs by itself; you don't need to keep anything open.

Good to know:
- **Timing:** runs are scheduled at :07 and :37 past each hour on weekdays. GitHub can start them a few
  minutes late on busy days, and the bot checks the real NYSE calendar, so weekends and holidays are
  skipped.
- **Stopping it:** Actions &rarr; *Stock bot (practice + Discord)* &rarr; **&middot;&middot;&middot;**
  &rarr; **Disable workflow** (turn it back on the same way). Deleting the Trading 212 secrets keeps
  the messages but stops all trading.
- **60-day rule:** GitHub pauses schedules in public repos after 60 days without any change to the repo.
  The daily summary warns you from day 50; any small edit (for example to this README) resets it.
- **The bot's memory** (its open orders and which shares it bought) is kept in GitHub's Actions cache
  between runs. GitHub deletes cache entries that aren't used for 7 days, so if the workflow is off for
  over a week the bot forgets which shares were its own; it then treats them as yours and never sells
  them.
- **Use one mode per practice account:** don't run this and the Oracle command center bot on the same
  account at the same time; each keeps its own record of the bot's shares.
- The daily loss limit is checked on every run (not only near the close), and once hit, no more buys
  that day.
- Workflow logs of a public repo can be seen by anyone; they show what the bot did but never your keys
  or balances.
- To try it on your own computer instead: put the same values in `.env` and run
  `python run_scheduled.py` (without `DISCORD_WEBHOOK_URL` it just prints the messages).

## Disclaimer

For education and paper trading only. Past performance (including backtests and famous investors'
trades) does not predict future results.
