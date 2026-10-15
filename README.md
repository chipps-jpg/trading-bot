# Options trade assistant (advisory only)

A self-hosted bot you drive from your phone. It does NOT place trades. You buy and sell
yourself (Fidelity, Webull, whatever) and tell the bot what you did. It then watches your
positions, warns you at -10% / -20%, flags +20% / +40% targets, and scans news for
buy / put signals using a local Ollama model.

Full step-by-step setup: see SETUP.md

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
