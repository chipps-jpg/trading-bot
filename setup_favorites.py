#!/usr/bin/env python3
"""Run once after installing: asks which 2 or 3 tickers you want to follow closely and saves them (favorites.json)."""
import re
import sys

from market_extras import load_favorites, save_favorites

cur, ok = load_favorites()
print("Which 2 or 3 tickers do you want to follow closely?")
print("They get the closest news watching and skip the 'one-off ticker' warnings.")
if ok:
    print("Currently:", " ".join(cur))
raw = input("Tickers, separated by spaces or commas (example: SPY NVDA): ")
tk = []
for w in re.split(r"[\s,]+", raw.strip().upper()):
    if re.fullmatch(r"[A-Z.]{1,6}", w) and w not in tk:
        tk.append(w)
if not tk:
    print("No tickers entered. Nothing changed.")
    sys.exit(0)
if len(tk) > 6:
    print("Using the first 6.")
    tk = tk[:6]
save_favorites(tk)
print("Saved:", " ".join(tk))
print("Restart the bots to pick it up:  sudo systemctl restart tradebot newswatch")
