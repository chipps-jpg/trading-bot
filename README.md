# Options trade assistant (advisory only)

## READ THIS FIRST - use at your own risk

This is a personal hobby project, shared as-is. It is **not financial advice**, it is not a
recommendation to buy or sell anything, and nobody has audited it. It can have bugs. Options
can lose 100% of what you put in. Only use money you can afford to lose, and paper-trade
(log pretend trades) for a while before using real money. The author is not responsible for
any losses or damages from using this software.

**The settings are the author's, not yours.** The rules, tickers, trading window, stops and
sizing in this code reflect one person's account and habits. Before you rely on it, go through
and change them to fit YOU:

- **Tickers:** `WATCHLIST` in tradebot.py and `my_tickers` in newswatch.py
- **Rules and trading window:** `RULES`, `TRADE_DAYS` and `DAY_START` in tradebot.py
  (the default is: Mon and Tue start at 11 AM Central, Wed-Fri all day, flat by Friday, long calls and puts only)
- **Stops and targets:** `STOP`, `TARGETS` and `SOFT` in tradebot.py (-20%, +20%/+40%, +/-10%)
- **Position size:** set with `balance` and the cash/slots settings (default 50% cash, 2 positions)
- **Fees:** `FEE_PER_CONTRACT` in tradebot.py ($0.65 each way is a Fidelity-style number)
- **Time zone:** `America/Chicago` in tradebot.py, newswatch.py and market_extras.py
- **Account type:** it assumes a cash account (T+1 settlement). Margin accounts work differently.

The bot only advises. It never places orders. You are responsible for every trade you make.

A self-hosted bot you drive from your phone. It does NOT place trades. You buy and sell
yourself (Fidelity, Webull, whatever) and tell the bot what you did. It then watches your
positions, warns you at -10% / -20%, flags +20% / +40% targets, and scans news for
buy / put signals using a local Ollama model.

Full step-by-step setup: see SETUP.md

Free to use. If it helps you, an optional tip is appreciated: https://venmo.com/u/chipps2069 <!-- tip -->

Built for long calls and puts only, in a cash account. Not financial advice. It can lose
you money. Use it at your own risk, and paper-trade first.

## What you need

- Linux or macOS, Python 3.10+
- Ollama (https://ollama.com) with the model:  `ollama pull qwen2.5:7b`  (about 5 GB)
- ntfy (https://ntfy.sh), self-hosted, listening on port 2586. Install the ntfy app on your phone
  and subscribe to two topics on your server: `alerts` (bot -> you) and `trade` (you -> bot).
- Optional: Alpaca free API keys for near-real-time prices (see alpaca.env.example)
- Optional: Tailscale so your phone can reach the server from anywhere

Anyone who can reach your ntfy server can send the bot commands. Keep it on a private
network (Tailscale or LAN) or turn on ntfy auth. Do not expose port 2586 to the internet.

## Install

    git clone <this repo> trading && cd trading
    python3 -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    cp alpaca.env.example alpaca.env && chmod 600 alpaca.env   # optional, then add your keys

## Run (two terminals, or make systemd services)

    python3 tradebot.py     # commands, alerts, position watcher
    python3 newswatch.py    # news scanner (needs Ollama running)

healthcheck.py is an optional watchdog meant for a systemd timer.

## First steps (text these to the `trade` topic)

    help
    balance 4500            # your starting balance; default is 50% cash, 2 positions
    pick SPY                # the specific call to look at
    buy SPY 750C 5/22 x5 @4.10      # log a buy
    list                    # open positions and P/L
    sell SPY 750C @4.80     # log a sell
    stats                   # win rate, profit factor, streaks

You can also paste an order straight from Fidelity (include the "Filled at $..." line):

    Buy to Open
    5 Contracts SPY Oct 26 2026 775 Calls Limit at $3.79 (Day)
    Filled at $2.72

## Things to change for your own use

- Time zone: `America/Chicago` is hard-coded in tradebot.py, newswatch.py and market_extras.py
- Trading window and rules: see RULES in tradebot.py (Mon/Tue after 11 AM, flat by Friday)
- Watchlist: WATCHLIST in tradebot.py, my_tickers in newswatch.py
- Fees: FEE_PER_CONTRACT in tradebot.py ($0.65 per contract each way)

## Your data stays local

positions, trade log, balance and signals are written next to the code. The .gitignore keeps
them out of git. Never commit alpaca.env.
