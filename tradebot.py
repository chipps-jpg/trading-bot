#!/usr/bin/env python3
"""
tradebot.py - Tim's trade log + alert bot, talking over self-hosted ntfy.

Phone -> topic "trade":   buy / sell / list / nvda / rules / drop / help
Bot   -> topic "trade":   confirmations and answers
Bot   -> topic "alerts":  tiered alerts (urgent / action / info)

Rulebook it enforces (warns, never blocks):
  1. Open trades Wed-Fri only; be flat by Friday close
  2. NVDA calls only in the pullback zone (not stretched above 10-day avg, RSI < 60)
  3. SPY entries as usual
  4. 9+ days to expiry; out at -20%

Prices come from Yahoo and are delayed ~15 minutes. Treat alerts as
"go look now", not as exact fills.
"""
import csv
import json
import os
import re
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from market_extras import earnings_line, earnings_before, next_earnings
from market_extras import contract_text
from market_extras import market_day, is_open_now, last_session_of_week, beat
LAST_QUOTE_OK = 0.0

# ---------------------------------------------------------------- settings
NTFY = "http://localhost:2586"
CMD_TOPIC = "trade"
ALERT_TOPIC = "alerts"
BOT_TITLE = "tradebot"
TZ = ZoneInfo("America/Chicago")

STOP = -0.20               # urgent alert at or below this
TARGETS = [0.20, 0.40]     # action alerts at these gains
SOFT = 0.10                # heads-up at +10% and -10%
STOP_REPEAT_MIN = 15       # re-ping while still below the stop
MIN_DTE = 9                # rule: days to expiry at entry
NVDA_MAX_EXT10 = 0.02      # pullback zone: <= 2% above 10-day avg
NVDA_MAX_RSI = 60          # ...and RSI(14) under 60
TRADE_DAYS = {0, 1, 2, 3, 4}  # Mon-Fri (Mon = 0)  # puts+monday v1
DAY_START = {0: dtime(11, 0), 1: dtime(11, 0)}  # Mon and Tue start at 11:00
HOT_DAYS = {2, 3, 4}       # the 12-for-12 SPY setup was Wed-Fri


def can_trade(n):
    return market_day(n.date())["open"] and n.weekday() in TRADE_DAYS and n.time() >= DAY_START.get(n.weekday(), dtime(0, 0))


def window_msg(n):
    md = market_day(n.date())
    if not md["open"] and n.weekday() < 5:
        return f"Market closed today ({md['name']})."
    if n.weekday() in DAY_START and n.time() < DAY_START[n.weekday()]:
        return f"{n:%A} trading opens at 11:00."
    return "Weekend - market's closed." if n.weekday() > 4 else "Outside your trading window."

WATCHLIST = {"SPY", "NVDA", "AAPL", "SOFI", "HOOD"}

POLL_SEC = 120             # position check interval during market hours
NVDA_POLL_MIN = 15         # pullback-zone check interval
MARKET_OPEN = dtime(8, 30)
MARKET_CLOSE = dtime(15, 0)

HERE = os.path.dirname(os.path.abspath(__file__))
POS_FILE = os.path.join(HERE, "positions.json")
LOG_FILE = os.path.join(HERE, "trade_log.csv")

PRI_URGENT, PRI_ACTION, PRI_INFO = 5, 4, 2
lock = threading.RLock()


# ---------------------------------------------------------------- ntfy
def _hdr(text):
    """Header-safe text: plain ASCII stays as is, anything else is RFC 2047 encoded (ntfy decodes it)."""
    import base64
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def send(topic, msg, title=None, priority=3, tags=None):
    headers = {"Priority": str(priority)}
    if title:
        headers["Title"] = _hdr(title)
    if tags:
        headers["Tags"] = tags
    try:
        requests.post(f"{NTFY}/{topic}", data=msg.encode("utf-8"), headers=headers, timeout=10)
    except Exception as e:
        print(f"[send failed] {e}")


def reply(msg):
    send(CMD_TOPIC, msg, title=BOT_TITLE, priority=3)


def alert(msg, title, priority, tags=None):
    send(ALERT_TOPIC, msg, title=title, priority=priority, tags=tags)


# ---------------------------------------------------------------- storage
def now():
    return datetime.now(TZ)


def load_positions():
    try:
        with open(POS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_positions(pos):
    tmp = POS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pos, f, indent=2)
    os.replace(tmp, POS_FILE)


def log_trade(action, pid, qty, price, pnl=""):
    new = not os.path.exists(LOG_FILE)  # log guard v1
    if not new:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            if f.tell():
                f.seek(-1, 2)
                if f.read(1) not in (b"\n", b"\r"):
                    with open(LOG_FILE, "a", newline="") as g:
                        g.write("\n")
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "action", "contract", "qty", "price", "realized_pnl"])
        w.writerow([now().isoformat(timespec="seconds"), action, pid, qty, price, pnl])


# ---------------------------------------------------------------- parsing
RE_CONTRACT = re.compile(r"^(\d+(?:\.\d+)?)([cp])$", re.I)
RE_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$")
RE_QTY = re.compile(r"^(?:x(\d+)|(\d+)x)$", re.I)
RE_PRICE = re.compile(r"^@(\d*\.?\d+)$")


def parse_expiry(m, today):
    mo, dy, yr = int(m.group(1)), int(m.group(2)), m.group(3)
    if yr:
        yr = int(yr)
        yr = yr + 2000 if yr < 100 else yr
        return date(yr, mo, dy)
    d = date(today.year, mo, dy)
    return d if d >= today else date(today.year + 1, mo, dy)


def parse_trade(words, today):
    """words after the verb: TICKER STRIKE[C|P] [M/D] [xQTY] [@PRICE] in any order after ticker."""
    if not words:
        raise ValueError("missing ticker")
    out = {"ticker": words[0].upper(), "strike": None, "cp": None,
           "expiry": None, "qty": None, "price": None}
    for w in words[1:]:
        if m := RE_CONTRACT.match(w):
            out["strike"], out["cp"] = float(m.group(1)), m.group(2).upper()
        elif m := RE_DATE.match(w):
            out["expiry"] = parse_expiry(m, today)
        elif m := RE_QTY.match(w):
            out["qty"] = int(m.group(1) or m.group(2))
        elif m := RE_PRICE.match(w):
            out["price"] = float(m.group(1))
        else:
            raise ValueError(f"didn't understand '{w}'")
    if out["strike"] is None:
        raise ValueError("missing strike, like 750C")
    return out


def fmt_strike(s):
    return f"{s:g}"


FEE_PER_CONTRACT = 0.65   # Fidelity commission per contract, each way - change if yours differs


def net_pct_usd(entry, price, qty):
    """P/L after fees on both the buy and the sell: (pct, dollars)."""
    cost = entry * qty * 100 + FEE_PER_CONTRACT * qty
    proceeds = price * qty * 100 - FEE_PER_CONTRACT * qty
    return (proceeds / cost - 1 if cost else 0.0), proceeds - cost


def level_price(entry, pct):
    """Sell price (per share) at which your P/L after fees equals pct."""
    return ((1 + pct) * (entry * 100 + FEE_PER_CONTRACT) + FEE_PER_CONTRACT) / 100


def pid_of(t, strike, cp, expiry):
    return f"{t} {fmt_strike(strike)}{cp} {expiry.month}/{expiry.day}"


# ---------------------------------------------------------------- market data
def option_mid(ticker, expiry_iso, strike, cp, cache=None):
    """Bid (the price you would sell at); last trade only when the market is closed. Yahoo, ~15 min delayed."""
    try:
        import alpaca_live
        q = alpaca_live.option_quote(ticker, expiry_iso, strike, cp)
        if q and q["bid"] > 0 and q["ask"] > 0 and q["age_min"] is not None and q["age_min"] <= 20:
            return q["bid"]
    except Exception as e:
        print(f"[alpaca option] {e}")
    import yfinance as yf
    key = (ticker, expiry_iso)
    if cache is not None and key in cache:
        chain = cache[key]
    else:
        chain = yf.Ticker(ticker).option_chain(expiry_iso)
        if cache is not None:
            cache[key] = chain
    df = chain.calls if cp == "C" else chain.puts
    row = df[(df["strike"] - strike).abs() < 1e-6]
    if row.empty:
        return None
    r = row.iloc[0]
    bid, ask, last = float(r.get("bid") or 0), float(r.get("ask") or 0), float(r.get("lastPrice") or 0)
    if bid > 0 and ask > 0:
        return bid          # what you would get selling, not the middle of the quote
    if not market_open(now()):
        return last or None  # market closed: show the last trade
    return None             # market open but no bid yet: do not guess


def rsi(series, n=14):
    d = series.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return float((100 - 100 / (1 + g / l)).iloc[-1])


def nvda_state():
    import yfinance as yf
    t = yf.Ticker("NVDA")
    closes = t.history(period="3mo", interval="1d", auto_adjust=True)["Close"].dropna()
    try:
        live = float(t.fast_info.last_price)
    except Exception:
        live = float(closes.iloc[-1])
    closes.index = closes.index.tz_convert(TZ) if closes.index.tz else closes.index.tz_localize(TZ)
    if closes.index[-1].date() == now().date():
        closes.iloc[-1] = live
    else:
        closes = pd.concat([closes, pd.Series([live], index=[pd.Timestamp(now())])])
    ext10 = live / closes.iloc[-10:].mean() - 1
    ext20 = live / closes.iloc[-20:].mean() - 1
    r = rsi(closes)
    zone = ext10 <= NVDA_MAX_EXT10 and r < NVDA_MAX_RSI
    avg10 = float(closes.iloc[-10:].mean())
    return {"price": live, "ext10": ext10, "ext20": ext20, "rsi": r, "zone": zone, "avg10": avg10}


def nvda_text(s):
    if s["zone"]:
        return f"\u2705 NVDA: OK to buy. It has cooled off.\nPrice ${s['price']:.2f}"
    why = []
    if s["ext10"] > NVDA_MAX_EXT10:
        why.append(f"it's {s['ext10']:.1%} above its 10-day average")
    if s["rsi"] >= NVDA_MAX_RSI:
        why.append(f"it's running hot (RSI {s['rsi']:.0f})")
    msg = f"\u274c NVDA: Don't buy. Not ready - {' and '.join(why)}.\nPrice ${s['price']:.2f}"
    if s["ext10"] > NVDA_MAX_EXT10 and "avg10" in s:
        msg += f" | ready around ${s['avg10'] * (1 + NVDA_MAX_EXT10):.2f} or lower"
    return msg



# ---------------------------------------------------------------- SPY hot buy
HOT_DTE = (9, 14)        # expiries to consider
HOT_OTM = 0.01           # strikes from at-the-money up to 1% above
HOT_MAX_SPREAD = 0.05    # skip contracts with bid/ask spread over 5%
HOT_MIN_OI = 500         # skip thinly traded strikes


def spy_hot(n=None):
    n = n or now()
    if not can_trade(n):
        return False, window_msg(n)
    if n.weekday() not in HOT_DAYS:
        return False, "Mon/Tue is OK to trade, but your hot setup is Wed-Fri."
    if n.weekday() == 4 and n.time() >= dtime(13, 0):
        return False, "Too late Friday - no time to work before the close."
    return True, "Wed-Fri, 9-14 days out, out by Friday. Your setup went 12 for 12."


def spy_picks(top=3):
    """Near-the-money SPY calls 9-14 days out with tight spreads. Yahoo, ~15 min delayed."""
    import yfinance as yf
    t = yf.Ticker("SPY")
    try:
        spot = float(t.fast_info.last_price)
    except Exception:
        spot = float(t.history(period="5d")["Close"].iloc[-1])
    today = now().date()
    rows = []
    for e in t.options:
        dte = (date.fromisoformat(e) - today).days
        if not HOT_DTE[0] <= dte <= HOT_DTE[1]:
            continue
        c = t.option_chain(e).calls
        c = c[(c["strike"] >= spot * 0.998) & (c["strike"] <= spot * (1 + HOT_OTM))]
        for _, r in c.iterrows():
            bid, ask = float(r.get("bid") or 0), float(r.get("ask") or 0)
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid
            oi = int(r.get("openInterest") or 0)
            if spread > HOT_MAX_SPREAD or oi < HOT_MIN_OI:
                continue
            score = abs(dte - 10) + abs(r["strike"] / spot - 1.003) * 300
            rows.append({"exp": date.fromisoformat(e), "dte": dte, "strike": float(r["strike"]),
                         "mid": mid, "spread": spread, "oi": oi, "score": score})
    rows.sort(key=lambda x: x["score"])
    return spot, rows[:top]


def spy_text(with_picks=True, budget=None):
    hot, why = spy_hot()
    head = f"\U0001F525 SPY: HOT BUY day. {why}" if hot else f"SPY: not a hot buy. {why}"
    if not with_picks or not can_trade(now()):
        return head
    try:
        spot, picks = spy_picks()
    except Exception as e:
        return head + f"\n(couldn't pull the option chain: {e})"
    if not picks:
        return head + f"\nSPY ${spot:.2f} - no liquid 9-14 day calls near the money right now."
    per = f" with ${budget:,.0f}" if budget else ""
    lines = [head, f"SPY ${spot:.2f}. Best fits for your setup{per}:"]
    first_qty = 5
    for i, k in enumerate(picks, 1):
        qty = int(budget // (k["mid"] * 100)) if budget else 5
        if i == 1:
            first_qty = max(qty, 1)
        if budget and qty < 1:
            lines.append(f"{i}. {fmt_strike(k['strike'])}C {k['exp'].month}/{k['exp'].day} ~${k['mid']:.2f} - "
                         f"one contract is ${k['mid'] * 100:,.0f}, over budget")
            continue
        cost = qty * k["mid"] * 100
        risk = cost * -STOP
        lines.append(f"{i}. {fmt_strike(k['strike'])}C {k['exp'].month}/{k['exp'].day} ({k['dte']}d) "
                     f"~${k['mid']:.2f} | x{qty} = ${cost:,.0f} | stop {k['mid'] * (1 + STOP):.2f} "
                     f"(risk ${risk:,.0f})")
    lines.append(f"To log: buy SPY {fmt_strike(picks[0]['strike'])}C {picks[0]['exp'].month}/{picks[0]['exp'].day} x{first_qty} @price")
    return "\n".join(lines)


# ---------------------------------------------------------------- rules
MIN_STOCK_PRICE = 5.0
MIN_AVG_VOLUME = 800_000


def catalyst(ticker, cp="C"):
    """Fresh news in your direction (bullish for calls, bearish for puts) in the last 3 days."""
    want = "bullish" if cp == "C" else "bearish"
    cutoff = (now() - timedelta(days=3)).date().isoformat()
    try:
        for r in _outcomes():
            if r["ticker"] == ticker and r["date"] >= cutoff and r["direction"] == want:
                return r["event"].replace("_", " ")
    except Exception:
        pass
    try:
        with open(os.path.join(HERE, "buy_signals.json")) as f:
            for x in json.load(f):
                if x["ticker"] == ticker and x.get("side", "C") == cp and x["time"][:10] >= cutoff:
                    return x["event"].replace("_", " ")
    except Exception:
        pass
    return None


def oneoff_problems(t):
    """Conditions an off-list ticker must meet. Returns the ones that failed."""
    bad = []
    try:
        import yfinance as yf
        fi = yf.Ticker(t["ticker"]).fast_info
        px = float(fi.last_price)
        vol = float(fi.three_month_average_volume or 0)
        if px < MIN_STOCK_PRICE:
            bad.append(f"stock is ${px:.2f}, under your ${MIN_STOCK_PRICE:g} floor")
        if vol < MIN_AVG_VOLUME:
            bad.append(f"only {vol / 1e6:.1f}M shares a day (want {MIN_AVG_VOLUME / 1e6:g}M+)")
    except Exception as e:
        bad.append(f"couldn't check stock price/volume ({e})")
    try:
        import yfinance as yf
        ch = yf.Ticker(t["ticker"]).option_chain(t["expiry"])
        df = ch.calls if t["cp"] == "C" else ch.puts
        row = df[(df["strike"] - float(t["strike"])).abs() < 1e-6]
        if row.empty:
            bad.append("couldn't find that contract to check liquidity")
        else:
            r = row.iloc[0]
            bid, ask = float(r.get("bid") or 0), float(r.get("ask") or 0)
            oi = int(r.get("openInterest") or 0)
            mid = (bid + ask) / 2
            if bid <= 0 or ask <= 0 or (ask - bid) / mid > 0.15:
                bad.append("wide bid/ask gap (hard to sell)")
            if oi < 100:
                bad.append(f"thin options (only {oi} open contracts)")
    except Exception as e:
        bad.append(f"couldn't check liquidity ({e})")
    if not catalyst(t["ticker"], t["cp"]):
        bad.append("no fresh " + ("good" if t["cp"] == "C" else "bad") + " news in the last 3 days")
    return bad


def rule_warnings(t, n):
    w = []
    dte = (date.fromisoformat(t["expiry"]) - n.date()).days
    if not can_trade(n):
        w.append(window_msg(n))
    if n.weekday() == 4 and n.time() >= dtime(14, 0):
        w.append("Late Friday - you need to be flat by the close.")
    if dte < MIN_DTE:
        w.append(f"Only {dte} days to expiry (rule is {MIN_DTE}+).")
    px = t.get("price")
    if px and px < 0.50:
        w.append(f"Cheap option (${px:.2f}): fees eat about {200 * FEE_PER_CONTRACT / (px * 100):.0f}% round trip "
                 f"and one 1-cent tick moves it {1 / (px * 100) * 100:.0f}%. Stops will whipsaw.")
    if t["ticker"] not in WATCHLIST:
        bad = oneoff_problems(t)
        if bad:
            w.append(f"{t['ticker']} isn't SPY or NVDA and doesn't meet the conditions: " + "; ".join(bad) + ".")
    if t["ticker"] == "SPY" and t["cp"] == "C" and spy_hot(n)[0] and HOT_DTE[0] <= dte <= HOT_DTE[1]:
        w.append("\U0001F525 Matches your hot SPY setup. Plan to be out by Friday.")
    if t["cp"] == "P" and t["ticker"] in WATCHLIST and not catalyst(t["ticker"], "P"):
        w.append(f"Put without bad news: nothing bearish on {t['ticker']} in the last 3 days. "
                 "Puts fight the market's upward drift, so they need a reason.")
    if t["ticker"] == "NVDA" and t["cp"] == "C":
        try:
            s = nvda_state()
            if not s["zone"]:
                w.append("NVDA: don't buy, not ready. Send 'nvda' for why.")
        except Exception as e:
            w.append(f"(couldn't check NVDA zone: {e})")
    try:
        eb = earnings_before(t["ticker"], t["expiry"], n.date())
        if eb:
            ex = date.fromisoformat(t["expiry"])
            w.append(f"\U0001F4C5 {t['ticker']} reports earnings {eb.month}/{eb.day}, before your {ex.month}/{ex.day} "
                     "expiry - options often drop after earnings even when you're right.")
    except Exception:
        pass
    return w



# ---------------------------------------------------------------- deal watch (shared with newswatch.py)
import fcntl
from contextlib import contextmanager
WATCH_FILE = os.path.join(HERE, "watchlist.json")
WATCH_LOCK = os.path.join(HERE, "watchlist.lock")


@contextmanager
def watch_locked():
    with open(WATCH_LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            with open(WATCH_FILE) as f:
                wl = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            wl = {}
        yield wl
        tmp = WATCH_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(wl, f, indent=2)
        os.replace(tmp, WATCH_FILE)


def cmd_watch(words):
    sub = words[0].lower() if words else "list"
    if sub == "add":
        if len(words) < 2:
            raise ValueError("which ticker? like: watch add BMBL")
        t = words[1].upper()
        note = " ".join(words[2:]) or "added by you"
        import yfinance as yf
        try:
            price = float(yf.Ticker(t).fast_info.last_price)
        except Exception:
            price = None
        with watch_locked() as wl:
            stamp = now().isoformat(timespec="seconds")
            wl[t] = {"added": stamp, "last_event": stamp, "status": "watching", "reason": note,
                     "price_added": price, "source": "manual", "events": [], "alerted": {}}
        reply(f"Watching {t}" + (f" from ${price:.2f}" if price else "") +
              ". You'll get pinged on unusual volume, heavy call buying, or deal news.")
        return
    if sub in ("drop", "remove", "rm"):
        if len(words) < 2:
            raise ValueError("which ticker? like: watch drop BMBL")
        t = words[1].upper()
        with watch_locked() as wl:
            found = wl.pop(t, None)
        reply(f"Dropped {t} from deal watch." if found else f"{t} wasn't on the watch list.")
        return
    with watch_locked() as wl:
        items = dict(wl)
    if not items:
        reply("Deal watch is empty. The news scanner adds tickers as it finds deal chatter, "
              "or use: watch add XYZ")
        return
    import yfinance as yf
    lines = ["Deal watch:"]
    for t, w in sorted(items.items(), key=lambda kv: kv[1]["added"]):
        days = (now() - datetime.fromisoformat(w["added"])).days
        chg = ""
        try:
            px = float(yf.Ticker(t).fast_info.last_price)
            if w.get("price_added"):
                chg = f" | ${px:.2f} ({px / w['price_added'] - 1:+.0%} since added)"
            else:
                chg = f" | ${px:.2f}"
        except Exception:
            pass
        flag = " [DEAL ANNOUNCED]" if w.get("status") == "announced" else ""
        lines.append(f"{t}{flag}: {w['reason']} | {days}d{chg}")
    reply("\n".join(lines))



def cmd_congress(words):
    path = os.path.join(HERE, "congress_log.csv")
    if not os.path.exists(path):
        reply("No Congress trades logged yet. They're checked every 2 hours.")
        return
    with open(path) as f:
        rows = list(csv.DictReader(f))
    if words:
        t = words[0].upper()
        rows = [r for r in rows if r["ticker"] == t or t.lower() in r["member"].lower()]
    rows = rows[-12:][::-1]
    if not rows:
        reply("Nothing logged for that.")
        return
    lines = ["Recent Congress buys (newest first):"]
    for r in rows:
        lines.append(f"{r['member']}: {r['ticker']} {r['amount']} traded {r['traded']} disclosed {r['disclosed']}")
    lines.append("Data: Bargo (bargo.ai)")
    reply("\n".join(lines))



def _outcomes():
    path = os.path.join(HERE, "news_outcomes.csv")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def cmd_news(words):
    rows = _outcomes()
    if words:
        t = words[0].upper()
        rows = [r for r in rows if r["ticker"] == t]
    rows = rows[-12:][::-1]
    if not rows:
        reply("No news logged yet" + (" for that ticker." if words else "."))
        return
    lines = ["Recent news (newest first):"]
    for r in rows:
        arrow = "+" if r["direction"] == "bullish" else ("-" if r["direction"] == "bearish" else " ")
        x3 = f" | 3d vs SPY {float(r['x3']):+.1%}" if r.get("x3") else ""
        lines.append(f"{arrow} {r['ticker']} {r['event'].replace('_', ' ')}: {r['title'][:80]}{x3}")
    reply("\n".join(lines))


def cmd_scoreboard(_):
    rows = [r for r in _outcomes() if r.get("x3")]
    if not rows:
        reply("No track record yet. Outcomes fill in after the close, 3 trading days after each event.")
        return
    stats = {}
    for r in rows:
        stats.setdefault(r["event"], []).append(float(r["x3"]))
    lines = ["Track record: avg 3-day move vs SPY after each news type", "(n = cases, % = beat SPY)"]
    for ev, xs in sorted(stats.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        avg = sum(xs) / len(xs)
        up = sum(1 for x in xs if x > 0) / len(xs)
        flag = "" if len(xs) >= 5 else " (thin)"
        lines.append(f"{ev.replace('_', ' ')}: {avg:+.1%} | {up:.0%} | n={len(xs)}{flag}")
    reply("\n".join(lines))



# ---------------------------------------------------------------- earnings + paper trades
_nvda_text_base = nvda_text


def nvda_text(s):
    try:
        return _nvda_text_base(s) + earnings_line("NVDA", now().date())
    except Exception:
        return _nvda_text_base(s)


def earnings_watch():
    today = now().date()
    for p in load_positions():
        try:
            d = next_earnings(p["ticker"], today)
        except Exception:
            continue
        if d and 0 <= (d - today).days <= 2 and d <= date.fromisoformat(p["expiry"]):
            when = "TODAY" if d == today else ("tomorrow" if (d - today).days == 1 else f"{d.month}/{d.day}")
            alert(f"{p['id']} x{p['qty']}: {p['ticker']} reports earnings {when}.\n"
                  "Options usually lose value right after earnings (IV crush), even if the stock moves your way. "
                  "Decide now: take it off, or hold on purpose.",
                  title=f"\U0001F4C5 Earnings {when}: {p['ticker']}", priority=PRI_ACTION, tags="calendar")


def cmd_paper(_):
    book_path, log_path = os.path.join(HERE, "paper.json"), os.path.join(HERE, "paper_log.csv")
    try:
        with open(book_path) as f:
            book = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        book = []
    rows = []
    if os.path.exists(log_path):
        with open(log_path) as f:
            rows = list(csv.DictReader(f))
    lines = ["Paper trades of buy signals (1 contract each, your rules)"]
    if rows:
        pcts = [float(r["pct"]) for r in rows]
        usd = sum(float(r["usd"]) for r in rows)
        wins = sum(1 for x in pcts if x > 0)
        lines.append(f"Closed: {len(rows)} | won {wins / len(rows):.0%} | avg {sum(pcts) / len(pcts):+.0%} | "
                     f"total {'+' if usd >= 0 else '-'}${abs(usd):,.0f}")
        by = {}
        for r in rows:
            by.setdefault(r["signal"], []).append(float(r["usd"]))
        for sig, v in sorted(by.items(), key=lambda kv: -sum(kv[1])):
            lines.append(f"  {sig.replace('_', ' ')}: {len(v)} trades, {'+' if sum(v) >= 0 else '-'}${abs(sum(v)):,.0f}")
        for r in rows[-3:][::-1]:
            lines.append(f"  last: {r['ticker']} {float(r['pct']):+.0%} ({r['reason']})")
    else:
        lines.append("No closed paper trades yet.")
    if book:
        lines.append(f"Open: " + ", ".join(f"{p['ticker']} {p['strike']:g}C" for p in book))
    lines.append("Quotes are Yahoo mid prices, ~15 min delayed - real fills would be a bit worse.")
    reply("\n".join(lines))



# ---------------------------------------------------------------- Friday scorecard
RULES_LOG = os.path.join(HERE, "rules_log.csv")
BREAK_KEYS = {
    "hands-off": "traded on a hands-off day",
    "opens at 11": "traded before 11 AM (Mon/Tue)",
    "Put without bad news": "put with no bad news",
    "AVERAGING DOWN": "averaged down on a loser",
    "Outside your trading window": "outside trading window",
    "Weekend": "outside trading window",
    "Late Friday": "opened late Friday",
    "days to expiry": "too few days to expiry",
    "isn't SPY or NVDA": "one-off ticker",
    "not ready": "NVDA bought while extended",
    "held past stop": "held past the -20% stop",
    "Cheap option": "cheap (<$0.50) option",
}


def log_rules(contract, notes, kind="buy"):
    breaks = []
    for n in notes:
        for key, label in BREAK_KEYS.items():
            if key in n and label not in breaks:
                breaks.append(label)
    new = not os.path.exists(RULES_LOG)
    with open(RULES_LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "contract", "kind", "breaks"])
        w.writerow([now().isoformat(timespec="seconds"), contract, kind, "; ".join(breaks)])


def scorecard(since=None, title="Scorecard"):
    trades = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            trades = list(csv.DictReader(f))
    rules = []
    if os.path.exists(RULES_LOG):
        with open(RULES_LOG) as f:
            rules = list(csv.DictReader(f))
    sells = [r for r in trades if r["action"] == "sell" and r["realized_pnl"]
             and (since is None or r["time"] >= since)]
    if not sells:
        held = load_positions()
        extra = f"\nStill open: {', '.join(p['id'] for p in held)}" if held else ""
        return f"{title}: no closed trades yet." + extra
    good, bad, reasons = [], [], {}
    for r in sells:
        pnl = float(r["realized_pnl"])
        br = set()
        for x in rules:
            if x["contract"] == r["contract"] and x["time"] <= r["time"] and x["breaks"]:
                # buys of this contract in the 10 days before the sale, and the sale itself
                if (datetime.fromisoformat(r["time"]) - datetime.fromisoformat(x["time"])).days <= 10:
                    br.update(b for b in x["breaks"].split("; ") if b)
        if br:
            bad.append(pnl)
            for b in br:
                reasons[b] = reasons.get(b, 0) + pnl
        else:
            good.append(pnl)

    def money(v):
        return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"

    total = sum(good) + sum(bad)
    wins = sum(1 for v in good + bad if v > 0)
    lines = [f"{title}: {len(sells)} closed, won {wins / len(sells):.0%}, {money(total)}",
             f"✅ Followed the rules: {len(good)} trades, {money(sum(good))}"
             + (f" (avg {money(sum(good) / len(good))})" if good else ""),
             f"❌ Broke a rule: {len(bad)} trades, {money(sum(bad))}"
             + (f" (avg {money(sum(bad) / len(bad))})" if bad else "")]
    for b, v in sorted(reasons.items(), key=lambda kv: kv[1]):
        lines.append(f"   - {b}: {money(v)}")
    held = load_positions()
    if held and now().weekday() == 4:
        lines.append("⚠️ Holding into the weekend: " + ", ".join(p["id"] for p in held)
                     + " (rule: flat by Friday)")
    return "\n".join(lines)


def week_start():
    n = now()
    monday = (n - timedelta(days=n.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.isoformat(timespec="seconds")


def friday_scorecard():
    body = scorecard(week_start(), "This week")
    body += "\n\n" + scorecard(None, "All time (since logging)")
    alert(body, title="\U0001F4CA Friday scorecard", priority=PRI_INFO, tags="bar_chart")


def cmd_scorecard(words):
    if words and words[0].lower() == "all":
        reply(scorecard(None, "All time (since logging)"))
    else:
        reply(scorecard(week_start(), "This week") + "\n(send 'scorecard all' for all time)")



# ---------------------------------------------------------------- account balance + sizing
ACCOUNT = os.path.join(HERE, "account.json")


def load_account():
    try:
        with open(ACCOUNT) as f:
            a = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        a = {}
    return {"balance": a.get("balance"), "cash_pct": a.get("cash_pct", 0.5), "slots": a.get("slots", 2)}


def save_account(a):
    tmp = ACCOUNT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(a, f)
    os.replace(tmp, ACCOUNT)


def auto_budget():
    a = load_account()
    if not a["balance"]:
        return None
    b = a["balance"] * (1 - a["cash_pct"]) / a["slots"]
    try:
        sc = settled_cash()
        if sc is not None:
            b = min(b, max(sc, 0))
    except Exception:
        pass
    return round(b, -1)


def bump_balance(pnl):
    a = load_account()
    if a["balance"] is not None:
        a["balance"] = round(a["balance"] + pnl, 2)
        save_account(a)


def sizing_line():
    a = load_account()
    b = auto_budget()
    if not b:
        return ""
    return (f"\nBudget per trade: ${b:,.0f} (balance ${a['balance']:,.0f}, "
            f"{a['cash_pct']:.0%} in cash, {a['slots']} positions max)")


def cmd_balance(words):
    a = load_account()
    if words and words[0].lower() in ("cash", "slots"):
        if len(words) < 2:
            raise ValueError("like: balance cash 50   or   balance slots 2")
        v = float(words[1].replace("%", ""))
        if words[0].lower() == "cash":
            a["cash_pct"] = v / 100 if v > 1 else v
        else:
            a["slots"] = max(1, int(v))
    elif words:
        a["balance"] = float(words[0].replace("$", "").replace(",", ""))
    else:
        if not a["balance"]:
            reply("No balance set. Send: balance 4500")
            return
    save_account(a)
    reply(f"Balance ${a['balance']:,.0f}. It moves up and down with each sell you log "
          "(before fees) - send 'balance 4720' any time to match Fidelity." + sizing_line())


# ---------------------------------------------------------------- commands
HELP = """Commands:
buy SPY 750C 5/22 x5 @4.10
sell SPY 750C @4.80        (all, or add x2 for part)
list    - open positions + P/L
nvda    - NVDA buy / don't buy
spy     - hot buy check + best contracts
pick AAPL - the specific call to buy (strike, date, size, stop)
spy 2500 - same, sized to your budget (plain 'spy' uses your balance)
balance 4500 - set balance; 'balance cash 50', 'balance slots 2'
rules   - the rulebook
cash    - settled cash you can use now and what settles when
stats   - profit/loss, win rate, profit factor, worst streak and drop
scorecard - this week: rule-following vs rule-breaking P&L
news    - recent news hits (news NVDA)
signal LLY - is the 🔥/🔻 signal still strong, fading or gone
scoreboard - which news types actually moved stocks
watch   - deal watch list (watch add XYZ / watch drop XYZ)
congress - recent Congress buys (congress NVDA / congress pelosi)
drop SPY 750C   - remove a mistaken entry (no P/L logged)"""

RULES = f"""Rulebook
1. Mon and Tue after 11 AM. Wed-Fri all day. Flat by Friday close.
2. NVDA calls only in the pullback zone.
3. SPY entries as usual.
4. {MIN_DTE}+ days to expiry. Out at {STOP:.0%}.
5. Any ticker is fine if the stock is $5+ and heavily traded, its options are liquid, and there is fresh news your way. Otherwise you get a warning.
6. Puts are single long puts only (same stop, same size) and need fresh bad news. 'pick TICKER put' shows them."""


def find_position(pos, t):
    hits = [p for p in pos if p["ticker"] == t["ticker"] and p["strike"] == t["strike"]
            and p["cp"] == t["cp"] and (t["expiry"] is None or p["expiry"] == t["expiry"].isoformat())]
    return hits


def cmd_buy(words):
    n = now()
    t = parse_trade(words, n.date())
    for k, label in [("expiry", "expiry like 5/22"), ("qty", "quantity like x5"), ("price", "price like @4.10")]:
        if t[k] is None:
            raise ValueError(f"missing {label}")
    t["expiry"] = t["expiry"].isoformat()
    exp = date.fromisoformat(t["expiry"])
    pid = pid_of(t["ticker"], t["strike"], t["cp"], exp)
    warnings = rule_warnings(t, n)
    warnings += cash_warning(t)
    with lock:
        pos = load_positions()
        existing = [p for p in pos if p["id"] == pid]
        if existing:
            p = existing[0]
            if t["price"] < p["entry"]:
                warnings.append(f"AVERAGING DOWN: you are buying more of a loser (avg {p['entry']:.2f}, now {t['price']:.2f}). "
                                "This is the hold-and-hope pattern. If you did not mean to, sell the add now.")
            total = p["qty"] + t["qty"]
            p["entry"] = round((p["entry"] * p["qty"] + t["price"] * t["qty"]) / total, 4)
            p["qty"] = total
            p["stop_last"], p["targets_hit"] = None, []
            note = f"Added to {pid}: now x{total} @ avg {p['entry']:.2f}"
        else:
            p = {"id": pid, "ticker": t["ticker"], "strike": t["strike"], "cp": t["cp"],
                 "expiry": t["expiry"], "qty": t["qty"], "entry": t["price"],
                 "opened": n.isoformat(timespec="seconds"),
                 "stop_last": None, "targets_hit": [], "expiry_warned": None}
            pos.append(p)
            note = f"Logged: {pid} x{t['qty']} @ {t['price']:.2f}"
        save_positions(pos)
        log_trade("buy", pid, t["qty"], t["price"])
        log_rules(pid, warnings)
    msg = (f"{note}\nStop alert at {level_price(p['entry'], STOP):.2f} ({STOP:.0%} after fees)"
           f" | +20% at {level_price(p['entry'], 0.20):.2f}")
    if warnings:
        msg += "\n\nRULE CHECK:\n- " + "\n- ".join(warnings)
    if any("hot SPY" in w for w in warnings) and len(warnings) == 1:
        msg = msg.replace("RULE CHECK:", "SETUP:")
    reply(msg)


def cmd_sell(words):
    t = parse_trade(words, now().date())
    if t["price"] is None:
        raise ValueError("missing price like @4.80")
    with lock:
        pos = load_positions()
        hits = find_position(pos, t)
        if not hits:
            raise ValueError("no open position matches that")
        if len(hits) > 1:
            raise ValueError("more than one expiry matches - add the date, like 5/22")
        p = hits[0]
        qty = t["qty"] or p["qty"]
        if qty > p["qty"]:
            raise ValueError(f"you only hold x{p['qty']}")
        pct, pnl = net_pct_usd(p["entry"], t["price"], qty)
        p["qty"] -= qty
        if p["qty"] == 0:
            pos.remove(p)
        save_positions(pos)
        log_trade("sell", p["id"], qty, t["price"], round(pnl, 2))
        bump_balance(pnl)
        add_unsettled(t["price"] * qty * 100)
        log_rules(p["id"], [f"held past stop: closed at {pct:.0%}"] if pct <= -0.25 else [], kind="sell")
    left = f" ({p['qty']} left)" if p["qty"] else ""
    reply(f"Closed {p['id']} x{qty} @ {t['price']:.2f}{left}\n{'+' if pnl >= 0 else '-'}${abs(pnl):,.0f} ({pct:+.0%}) after fees"
          + sizing_line() + settle_note(t["price"] * qty * 100))


def cmd_drop(words):
    t = parse_trade(words, now().date())
    with lock:
        pos = load_positions()
        hits = find_position(pos, t)
        if len(hits) != 1:
            raise ValueError("need exactly one match - add the date if you hold two expiries")
        pos.remove(hits[0])
        save_positions(pos)
        log_trade("drop", hits[0]["id"], hits[0]["qty"], "")
    reply(f"Dropped {hits[0]['id']} (no P/L logged)")


def position_lines(pos, cache):
    lines = []
    for p in pos:
        try:
            mid = option_mid(p["ticker"], p["expiry"], p["strike"], p["cp"], cache)
        except Exception:
            mid = None
        dte = (date.fromisoformat(p["expiry"]) - now().date()).days
        if mid:
            pct, usd = net_pct_usd(p["entry"], mid, p["qty"])
            lines.append(f"{p['id']} x{p['qty']} @ {p['entry']:.2f} -> {mid:.2f} bid "
                         f"{pct:+.0%} ({'+' if usd >= 0 else '-'}${abs(usd):,.0f}) | {dte}d left")
        else:
            lines.append(f"{p['id']} x{p['qty']} @ {p['entry']:.2f} -> no quote | {dte}d left")
    return lines


def cmd_list(_):
    pos = load_positions()
    if not pos:
        reply("No open positions.")
        return
    reply("Open positions (sell price = bid):\n" + "\n".join(position_lines(pos, {})))


def cmd_nvda(_):
    reply(nvda_text(nvda_state()) + sizing_line())


def cmd_spy(words):
    budget = auto_budget()
    if words:
        try:
            budget = float(words[0].replace("$", "").replace(",", "").lower().replace("k", "")) * \
                (1000 if words[0].lower().endswith("k") else 1)
        except ValueError:
            raise ValueError("budget should be a number, like: spy 2500")
    reply(spy_text(budget=budget))


def _history(rows, ev):
    xs = [float(r["x3"]) for r in rows if r["event"] == ev and r.get("x3")]
    if len(xs) < 3:
        return "no track record yet"
    avg = sum(xs) / len(xs)
    up = sum(1 for x in xs if x > 0) / len(xs)
    return f"past cases: {avg:+.1%} vs SPY after 3d, {up:.0%} beat it (n={len(xs)})"


def _nd(r):  # news dates v1
    try:
        d = datetime.strptime(r["date"][:10], "%Y-%m-%d")
        return f"{d:%b} {d.day}"
    except Exception:
        return "?"


def cmd_news_verdicts(words):
    rows = _outcomes()
    if not rows:
        reply("No news logged yet.")
        return
    if words:
        t = words[0].upper()
        mine = [r for r in rows if r["ticker"] == t][-6:][::-1]
        if not mine:
            reply(f"No news logged for {t}.")
            return
        lines = [f"{t} news (newest first):"]
        for r in mine:
            mark = {"bullish": "GOOD", "bearish": "BAD"}.get(r["direction"], "neutral")
            lines.append(f"{_nd(r)} - {mark}: {r['event'].replace('_', ' ')} - {r['title'][:80]}")
            lines.append(f"   {_history(rows, r['event'])}")
        reply("\n".join(lines))
        return
    cutoff = (now() - timedelta(days=3)).date().isoformat()
    recent = [r for r in rows if r["date"] >= cutoff] or rows[-12:]
    try:
        with open(os.path.join(HERE, "buy_signals.json")) as f:
            sigs = [x for x in json.load(f) if x["time"][:10] >= cutoff]
            fired = {x["ticker"] for x in sigs if x.get("side", "C") == "C"}
            fired_p = {x["ticker"] for x in sigs if x.get("side") == "P"}
    except (FileNotFoundError, json.JSONDecodeError):
        fired, fired_p = set(), set()
    by = {}
    for r in recent:
        by.setdefault(r["ticker"], []).append(r)
    out = []
    for t, rs in by.items():
        score = 0
        for r in rs:
            st = int(float(r.get("strength") or 1))
            score += st if r["direction"] == "bullish" else (-st if r["direction"] == "bearish" else 0)
        has_up = any(r["direction"] == "bullish" for r in rs)
        has_dn = any(r["direction"] == "bearish" for r in rs)
        if has_up and has_dn and abs(score) <= 1:
            verdict, icon = "MIXED", "\U0001F7E1"
        elif t in fired and score > 0:
            verdict, icon = "BUY SIGNAL", "\U0001F525"
        elif score > 0:
            verdict, icon = "GOOD", "\U0001F7E2"
        elif t in fired_p and score < 0:
            verdict, icon = "PUT SIGNAL", "\U0001F53B"
        elif score < 0:
            verdict, icon = "BAD", "\U0001F534"
        else:
            verdict, icon = "NEUTRAL", "\u26AA"
        top = max(rs, key=lambda r: (abs(int(float(r.get("strength") or 1))), r["time"]))
        out.append((abs(score), t, verdict, icon, top, len(rs)))
    out.sort(key=lambda x: -x[0])
    good = sum(1 for o in out if o[2] in ("GOOD", "BUY SIGNAL"))
    bad = sum(1 for o in out if o[2] in ("BAD", "PUT SIGNAL"))
    lines = [f"Last 3 days: {good} good, {bad} bad, {len(out) - good - bad} mixed/neutral"]
    for _, t, verdict, icon, top, n in out[:8]:
        more = f" (+{n - 1} more)" if n > 1 else ""
        lines.append(f"{icon} {t} - {verdict}: {top['event'].replace('_', ' ')}{more} ({_nd(top)})")
        lines.append(f"   {top['title'][:80]}")
        lines.append(f"   {_history(rows, top['event'])}")
    lines.append("\U0001F525 = the bot fired a buy signal. \U0001F53B = it fired a put signal. 'news NVDA' = one stock.")
    reply("\n".join(lines))


def cmd_pick(words):
    if not words:
        raise ValueError("like: pick AAPL   or   pick AAPL 800")
    t = words[0].upper()
    budget, cp = None, "C"
    for w in words[1:]:
        if w.lower() in ("put", "puts", "p"):
            cp = "P"
        elif w.lower() in ("call", "calls", "c"):
            cp = "C"
        else:
            budget = float(w.replace("$", "").replace(",", ""))
    txt = contract_text(t, budget, cp=cp)
    notes = []
    if t not in WATCHLIST or cp == "P":
        c = catalyst(t, cp)
        kind = "bad" if cp == "P" else "good"
        notes.append(f"\nNews check: {c} in the last 3 days." if c else
                     f"\nNo fresh {kind} news on {t} in the last 3 days - this is a no-catalyst trade.")
    try:
        eb = next_earnings(t, now().date())
        if eb and 0 <= (eb - now().date()).days <= 21:
            notes.append(f"\nEarnings {eb.month}/{eb.day} - options get repriced after.")
    except Exception:
        pass
    if not can_trade(now()):
        notes.append("\n" + window_msg(now()))
    reply(txt.lstrip("\n") + "".join(notes) + "\nTo log: buy " + t + " <strike>" + cp + " <date> x<qty> @price")


def cmd_stats(_):
    rows = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            rows = [r for r in csv.DictReader(f) if r["action"] == "sell" and r["realized_pnl"]]
    if not rows:
        reply("No closed trades logged yet. Stats appear after your first 'sell'.")
        return
    rows.sort(key=lambda r: r["time"])
    pnl = [float(r["realized_pnl"]) for r in rows]
    n = len(pnl)
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    total = sum(pnl)
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = sum(losses) / len(losses) if losses else 0
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None
    cum = peak = dd = 0.0
    streak = worst = 0
    for x in pnl:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)

    def m(v):
        return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"

    lines = [f"Stats: {n} closed trades since {rows[0]['time'][:10]}",
             f"Profit/loss: {m(total)} | avg per trade {m(total / n)}",
             f"Win rate {len(wins) / n:.0%} | avg win {m(avg_w)} | avg loss {m(avg_l)}"]
    if pf is not None:
        lines.append(f"Profit factor {pf:.2f} (over 1.0 = winners outweigh losers)")
    lines.append(f"Best {m(max(pnl))} | worst {m(min(pnl))}")
    lines.append(f"Longest losing streak {worst} | biggest drop from a high {m(dd)}")
    a = load_account() if "load_account" in globals() else {}
    if a.get("balance"):
        lines.append(f"Balance ${a['balance']:,.0f}")
    if n < 30:
        lines.append(f"Too early to judge: {n} of 30+ trades. Small samples swing wildly.")
    elif total > 0 and (pf or 0) >= 1.2:
        lines.append("After 30+ trades: positive with winners outweighing losers.")
    elif total > 0:
        lines.append("After 30+ trades: positive but thin. Keep going.")
    else:
        lines.append("After 30+ trades: negative. Check 'scorecard all' - are you following the rules?")
    reply("\n".join(lines))


# ---------------------------------------------------------------- settled cash (cash account)
UNSETTLED = os.path.join(HERE, "unsettled.json")


def _load_unsettled():
    try:
        with open(UNSETTLED) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def settle_date(d):
    """Options sales settle the next business day."""
    d = d + timedelta(days=1)
    while not market_day(d)["open"]:
        d += timedelta(days=1)
    return d


def add_unsettled(amount):
    today = now().date()
    items = [i for i in _load_unsettled() if i["settles"] > today.isoformat()]
    items.append({"amount": round(amount, 2), "settles": settle_date(today).isoformat()})
    with open(UNSETTLED, "w") as f:
        json.dump(items, f)


def settle_note(amount):
    d = settle_date(now().date())
    return f"\nProceeds ${amount:,.0f} settle {d.strftime('%a')} {d.month}/{d.day}."


def cash_status():
    a = load_account()
    if not a["balance"]:
        return None
    today = now().date().isoformat()
    unsettled = [i for i in _load_unsettled() if i["settles"] > today]
    invested = sum(p["entry"] * p["qty"] * 100 for p in load_positions())
    uns_total = sum(i["amount"] for i in unsettled)
    return {"balance": a["balance"], "invested": invested, "unsettled": unsettled,
            "unsettled_total": uns_total, "settled": a["balance"] - invested - uns_total}


def settled_cash():
    st = cash_status()
    return None if st is None else st["settled"]


def cash_warning(t):
    st = cash_status()
    if st is None or not st["unsettled"]:
        return []
    cost = t["qty"] * t["price"] * 100
    if cost <= max(st["settled"], 0):
        return []
    d = date.fromisoformat(min(i["settles"] for i in st["unsettled"]))
    return [f"Cash check: this costs ${cost:,.0f} but only ${max(st['settled'], 0):,.0f} is settled. "
            f"${st['unsettled_total']:,.0f} settles {d.strftime('%a')} {d.month}/{d.day}. Selling this before "
            "that money settles can be a good faith violation."]


def cmd_cash(_):
    st = cash_status()
    if st is None:
        reply("No balance set. Send: balance 4500")
        return
    lines = [f"Balance ${st['balance']:,.0f} | in open positions ${st['invested']:,.0f}",
             f"Settled cash you can use now: ${max(st['settled'], 0):,.0f}"]
    for i in sorted(st["unsettled"], key=lambda x: x["settles"]):
        d = date.fromisoformat(i["settles"])
        lines.append(f"  ${i['amount']:,.0f} from a sale settles {d.strftime('%a')} {d.month}/{d.day}")
    if not st["unsettled"]:
        lines.append("Everything is settled.")
    reply("\n".join(lines))


COMMANDS = {"buy": cmd_buy, "sell": cmd_sell, "drop": cmd_drop, "list": cmd_list,
            "status": cmd_list, "cash": cmd_cash, "stats": cmd_stats, "pick": cmd_pick, "live": lambda w: cmd_live(w), "signal": lambda w: cmd_signal(w), "balance": cmd_balance, "scorecard": cmd_scorecard, "scoreboard": cmd_scoreboard, "news": cmd_news_verdicts, "nvda": cmd_nvda, "spy": cmd_spy, "watch": cmd_watch, "congress": cmd_congress,
            "rules": lambda _: reply(RULES), "help": lambda _: reply(HELP)}


FID_RE = re.compile(
    r"(buy|sell)\s+to\s+(open|close)\D*?(\d+)\s+contracts?\s+([A-Za-z.]+)\s+([A-Za-z]{3})[a-z]*\s+(\d{1,2})\s+(\d{4})"
    r"\s+(\d[\d,]*(?:\.\d+)?)\s+(calls?|puts?)", re.I)  # fid comma v1
FID_PRICE = re.compile(r"(?:limit\s+at|filled\s+at|fill\s+at|@)\s*\$?(\d*\.?\d+)", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_fidelity(text):
    """Turn a pasted Fidelity order into (command, words), or None if it is not one."""
    m = FID_RE.search(text)
    if not m:
        if re.search(r"(buy|sell)\s+to\s+(open|close)", text, re.I):
            raise ValueError("I couldn't read that order. Paste the whole line, like: "
                             "Sell to Close 62 Contracts SOFI Oct 23 2026 16 Calls Limit at $0.15")
        return None
    side, oc, qty, tk, mon, day, _yr, strike, cp = m.groups()
    side, oc = side.lower(), oc.lower()
    if (side, oc) not in (("buy", "open"), ("sell", "close")):
        raise ValueError("The bot only tracks long options (Buy to Open, Sell to Close).")
    prices = FID_PRICE.findall(text)
    if not prices:
        raise ValueError("No price in that order (market order?). Add what it filled at, like: @0.15 at the end.")
    mo = MONTHS.get(mon.lower())
    if not mo:
        raise ValueError(f"Didn't understand the month '{mon}'.")
    words = [tk.upper(), f"{float(strike.replace(',', '')):g}{cp[0].upper()}", f"{mo}/{int(day)}", f"x{int(qty)}", f"@{prices[-1]}"]
    return side, words


def handle(text):
    try:
        fid = parse_fidelity(text)
    except ValueError as e:
        reply(f"Couldn't do that: {e}")
        return
    if fid:
        text = fid[0] + " " + " ".join(fid[1])
    words = text.replace(",", " ").split()
    if not words:
        return
    fn = COMMANDS.get(words[0].lower())
    if not fn:
        reply(f"Didn't recognize '{words[0]}'.\n\n{HELP}")
        return
    try:
        fn(words[1:])
    except ValueError as e:
        reply(f"Couldn't do that: {e}\n\nExample: buy SPY 750C 5/22 x5 @4.10")
    except Exception as e:
        traceback.print_exc()
        reply(f"Error: {e}")


# ---------------------------------------------------------------- listener
def listen():
    while True:
        try:
            with requests.get(f"{NTFY}/{CMD_TOPIC}/json", stream=True, timeout=(10, 90)) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    m = json.loads(line)
                    if m.get("event") != "message" or m.get("title") == BOT_TITLE:
                        continue
                    print(f"[cmd] {m.get('message')}")
                    handle(m.get("message", ""))
        except Exception as e:
            print(f"[listen] reconnecting: {e}")
            time.sleep(5)


# ---------------------------------------------------------------- scheduled checks
def market_open(n):
    return is_open_now(n)


def check_positions():
    n = now()
    cache = {}
    with lock:
        pos = load_positions()
        changed = False
        for p in pos:
            try:
                mid = option_mid(p["ticker"], p["expiry"], p["strike"], p["cp"], cache)
            except Exception as e:
                print(f"[quote] {p['id']}: {e}")
                continue
            if not mid:
                continue
            globals()["LAST_QUOTE_OK"] = time.time()
            pct, usd = net_pct_usd(p["entry"], mid, p["qty"])
            if pct <= STOP:
                last = datetime.fromisoformat(p["stop_last"]) if p["stop_last"] else None
                if not last or n - last >= timedelta(minutes=STOP_REPEAT_MIN):
                    again = " (still)" if last else ""
                    alert(f"{p['id']} is {pct:+.0%}{again}  (${abs(usd):,.0f} down)\n"
                          f"{p['entry']:.2f} -> {mid:.2f} (bid). Rule says sell now.",
                          title=f"SELL: {p['ticker']} hit {STOP:.0%}", priority=PRI_URGENT,
                          tags="rotating_light")
                    p["stop_last"] = n.isoformat(timespec="seconds")
                    changed = True
            elif p["stop_last"] and pct > STOP:
                p["stop_last"] = None
                changed = True
            soft = p.setdefault("soft", [])
            if pct >= SOFT and "up" not in soft:
                if pct < TARGETS[0]:
                    alert(f"{p['id']} is {pct:+.0%} (+${usd:,.0f})\n{p['entry']:.2f} -> {mid:.2f} (bid). "
                          "Maybe time to sell, or at least decide where you'd get out.",
                          title=f"{p['ticker']} up {pct:.0%}", priority=PRI_ACTION, tags="chart_with_upwards_trend")
                soft.append("up")
                changed = True
            elif pct < SOFT / 2 and "up" in soft:
                soft.remove("up")
                changed = True
            if pct <= -SOFT and "down" not in soft:
                if pct > STOP:
                    alert(f"{p['id']} is {pct:+.0%} (-${abs(usd):,.0f})\n{p['entry']:.2f} -> {mid:.2f} (bid). "
                          "Halfway to your stop. Maybe time to sell. Hold only if you would buy it again today.",
                          title=f"{p['ticker']} down {abs(pct):.0%}", priority=PRI_ACTION, tags="warning")
                soft.append("down")
                changed = True
            elif pct > -SOFT / 2 and "down" in soft:
                soft.remove("down")
                changed = True
            new_hits = [tgt for tgt in TARGETS if pct >= tgt and tgt not in p["targets_hit"]]
            if new_hits:
                top = max(new_hits)
                alert(f"{p['id']} is {pct:+.0%} (+${usd:,.0f})\n{p['entry']:.2f} -> {mid:.2f} (bid). " +
                      ("Second target hit. Take it, or decide your exit now." if top >= 0.4 else
                       "First target hit. Take it, or decide your exit now."),
                      title=f"Profit: {p['ticker']} +{top:.0%}", priority=PRI_ACTION,
                      tags="moneybag")
                p["targets_hit"].extend(new_hits)
                changed = True
        if changed:
            save_positions(pos)


def live_watch():
    import alpaca_live
    alpaca_live.watch(load_positions(), alert, now())


def cmd_live(words):
    import alpaca_live
    pos = load_positions()
    extra = [w.upper() for w in (words or []) if w.isalpha()]
    syms = sorted({p["ticker"] for p in pos} | set(extra or ["SPY"]))
    thr = 0.0015

    def arrow(x):
        return "?" if x is None else ("\u25B2" if x > thr else "\u25BC" if x < -thr else "\u25AC")

    def pct(x):
        return "n/a" if x is None else f"{x:+.1%}"

    try:
        tr = alpaca_live.trend(syms)
    except Exception as e:
        print(f"[live] trend: {e}")
        tr = {}
    if tr:
        lines = ["Source: Alpaca (live)"]
        for s in syms:
            d = tr.get(s)
            if not d:
                lines.append(f"{s}: no data")
                continue
            lines.append(f"{s} ${d['price']:.2f}  {arrow(d['day'])} {pct(d['day'])} vs yesterday's close")
            lines.append(f"   15 min {arrow(d['m15'])} {pct(d['m15'])} | 1 hr {arrow(d['m60'])} {pct(d['m60'])}"
                         f" | since open {arrow(d['open'])} {pct(d['open'])}")
            m15, m60 = d["m15"], d["m60"]
            if m15 is not None and m60 is not None and m15 > thr and m60 > thr:
                lines.append("   Trend: UP")
            elif m15 is not None and m60 is not None and m15 < -thr and m60 < -thr:
                lines.append("   Trend: DOWN")
            elif m60 is None:
                lines.append("   Trend: not enough recent data")
            else:
                lines.append("   Trend: choppy / flat")
        reply("\n".join(lines))
        return
    px = alpaca_live.prices(syms)
    if not px:
        reply("Couldn't get any prices (Alpaca and Yahoo both failed).")
        return
    lines = [f"Source: {alpaca_live.LAST_SOURCE['name']} (no trend data)"]
    lines += [f"{k} ${v:.2f}" for k, v in px.items()]
    reply("\n".join(lines))


# signal cmd v1
def cmd_signal(words):
    import alpaca_live
    pos = load_positions()
    tickers = [w.upper() for w in (words or []) if w.isalpha()] or sorted({p["ticker"] for p in pos})
    if not tickers:
        reply("Usage: signal LLY   (plain 'signal' checks the tickers you hold)")
        return
    try:
        with open(os.path.join(HERE, "buy_signals.json")) as f:
            sigs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        sigs = []
    rows = _outcomes()
    try:
        tr = alpaca_live.trend(tickers)
    except Exception as e:
        print(f"[signal] trend: {e}")
        tr = {}
    thr = 0.0015
    is_open = market_open(now())
    out = []
    for t in tickers:
        mine = [x for x in sigs if x.get("ticker") == t]
        if not mine:
            out.append(f"{t}: no signal logged. Try 'news {t}' or 'live {t}'.")
            continue
        sg = mine[-1]
        side = sg.get("side", "C")
        sign = 1 if side == "C" else -1
        word = "CALL" if side == "C" else "PUT"
        when = datetime.fromisoformat(sg["time"])
        if when.tzinfo is None:
            when = when.replace(tzinfo=now().tzinfo)
        mins = max(0, int((now() - when).total_seconds() // 60))
        age = f"{mins} min" if mins < 90 else (f"{mins // 60} hr" if mins < 2880 else f"{mins // 1440} days")
        ev = str(sg.get("event", "")).replace("_", " ")
        lines = [f"{t} {word} signal fired {age} ago ({ev})"]
        sd = when.date().isoformat()
        fav = against = 0
        for r in rows:
            if r["ticker"] == t and r["date"] >= sd:
                st = int(float(r.get("strength") or 1))
                if r["direction"] == ("bullish" if side == "C" else "bearish"):
                    fav += st
                elif r["direction"] in ("bullish", "bearish"):
                    against += st
        d = tr.get(t)
        if not d:
            lines.append("   Couldn't get a live price right now.")
            out.append("\n".join(lines))
            continue
        day = None if d["day"] is None else d["day"] * sign
        m15 = 0.0 if d["m15"] is None else d["m15"] * sign
        m60 = 0.0 if d["m60"] is None else d["m60"] * sign
        lines.append(f"   ${d['price']:.2f}, {'' if day is None else f'{day:+.1%}'} on the day in the signal's direction")
        lines.append(f"   last hour {m60:+.1%} | last 15 min {m15:+.1%}")
        lines.append(f"   news since: {fav} for, {against} against")
        if day is None:
            verdict = "UNKNOWN"
        elif day >= 0.01 and m60 >= 0 and m15 >= -thr and against <= fav:
            verdict = "STRONG"
        elif day <= 0 or (against > fav and day < 0.01) or m60 < -0.005 and day < 0.01:
            verdict = "GONE"
        else:
            verdict = "FADING"
        icon = {"STRONG": "\U0001F525", "FADING": "\U0001F7E1", "GONE": "\U0001F6D1"}.get(verdict, "?")
        lines.append(f"   {icon} {verdict}" + ("" if is_open else " (market closed - last session's picture)"))
        held = [q for q in pos if q["ticker"] == t]
        if held:
            lines.append(f"   You hold {held[0]['id']} - 'list' for P/L. Exits are still your targets and stop, not this.")
        out.append("\n".join(lines))
    reply("\n\n".join(out))


def check_expiries():
    today = now().date()
    with lock:
        pos = load_positions()
        for p in pos:
            dte = (date.fromisoformat(p["expiry"]) - today).days
            if dte <= 3 and p.get("expiry_warned") != today.isoformat():
                alert(f"{p['id']} expires in {dte} day(s). Time decay speeds up from here.",
                      title="Expiry close", priority=PRI_ACTION, tags="hourglass")
                p["expiry_warned"] = today.isoformat()
        save_positions(pos)


def morning_brief():
    n = now()
    pos = load_positions()
    held = f"\nOpen positions: {len(pos)}" if pos else ""
    md = market_day(n.date())
    if not md["open"]:
        if n.weekday() < 5:
            alert(f"Market closed today ({md['name']}). No trading.{held}", title=md["name"],
                  priority=PRI_INFO, tags="palm_tree")
        return
    if md["early"]:
        alert(f"Early close today: market closes at {md['close'].strftime('%-I:%M %p')} CT. "
              f"Plan to be out before then.", title="Short day", priority=PRI_INFO, tags="hourglass")
    if n.weekday() in (0, 1):
        alert(f"Trading opens at 11:00. No new trades before then.{held}",
              title=f"{n:%A}", priority=PRI_INFO, tags="clock11")
        return
    if n.weekday() in TRADE_DAYS:
        try:
            s = nvda_state()
            body = spy_text() + "\n\n" + nvda_text(s) + held
            pri = PRI_ACTION
        except Exception as e:
            body, pri = spy_text() + f"\n\nNVDA check failed: {e}{held}", PRI_ACTION
        alert(body, title=f"{n:%A} - trading day", priority=pri, tags="sunrise")
    else:
        alert(f"Hands-off day. No new trades.{held}", title=f"{n:%A}", priority=PRI_INFO, tags="zzz")


def tuesday_open():
    if not market_day(now().date())["open"]:
        return
    pos = load_positions()
    held = f"\nOpen positions: {len(pos)}" if pos else ""
    try:
        nv = nvda_text(nvda_state())
    except Exception as e:
        nv = f"NVDA check failed: {e}"
    alert(spy_text() + "\n\n" + nv + held, title=f"{now():%A} - trading open", priority=PRI_ACTION, tags="bell")


_FLAT_SENT = {"day": None}


def friday_flat():
    pos = load_positions()
    if pos:
        n = now()
        md = market_day(n.date())
        why = "last trading day this week" if n.weekday() != 4 else "Friday"
        if md["early"]:
            why += f", early close at {md['close'].strftime('%-I:%M %p')}"
        alert("Still holding:\n" + "\n".join(p["id"] + f" x{p['qty']}" for p in pos) +
              f"\nRule: flat before the weekend ({why}). 30 minutes left.",
              title="Get flat - market closes soon", priority=PRI_URGENT, tags="warning")


def flat_check(n):
    md = market_day(n.date())
    if not md["open"] or _FLAT_SENT["day"] == n.date() or not last_session_of_week(n.date()):
        return
    mins = (md["close"].hour * 60 + md["close"].minute) - (n.hour * 60 + n.minute)
    if 0 < mins <= 30:
        _FLAT_SENT["day"] = n.date()
        friday_flat()


def end_of_day():
    pos = load_positions()
    today = now().date().isoformat()
    trades = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            trades = [r for r in csv.DictReader(f) if r["time"].startswith(today)]
    if not pos and not trades:
        return
    realized = sum(float(r["realized_pnl"]) for r in trades if r["realized_pnl"])
    body = f"Trades today: {len(trades)} | realized {'+' if realized >= 0 else '-'}${abs(realized):,.0f}"
    if pos:
        body += "\n" + "\n".join(position_lines(pos, {}))
    alert(body, title="End of day", priority=PRI_INFO, tags="memo")


class Daily:
    """Run fn once per day at/after hh:mm on the given weekdays."""
    STATE = os.path.join(HERE, "daily_state.json")

    def __init__(self, hh, mm, fn, days=range(5)):
        self.at, self.fn, self.days, self.last = dtime(hh, mm), fn, set(days), None

    def _sent_today(self, n):
        # remembered on disk so a restart does not resend today's alert
        try:
            with open(self.STATE) as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        today = n.date().isoformat()
        if st.get(self.fn.__name__) == today:
            return True
        st[self.fn.__name__] = today
        try:
            with open(self.STATE, "w") as f:
                json.dump(st, f)
        except OSError:
            pass
        return False

    def tick(self, n):
        if n.weekday() in self.days and n.time() >= self.at and self.last != n.date():
            self.last = n.date()
            if self._sent_today(n):
                return
            if n.time() < (datetime.combine(n.date(), self.at) + timedelta(hours=2)).time():
                run(self.fn)


def run(fn):
    try:
        fn()
    except Exception:
        traceback.print_exc()


def main():
    print(f"tradebot up. commands on '{CMD_TOPIC}', alerts on '{ALERT_TOPIC}'")
    threading.Thread(target=listen, daemon=True).start()
    reply("tradebot online. Send 'help' for commands.")
    daily = [Daily(8, 0, morning_brief), Daily(8, 35, check_expiries), Daily(15, 30, friday_scorecard, days=[4]), Daily(8, 40, earnings_watch), Daily(11, 0, tuesday_open, days=[0, 1]),
             Daily(15, 10, end_of_day)]
    last_pos = last_nvda = last_live = 0.0
    nvda_zone = None
    while True:
        n = now()
        for d in daily:
            d.tick(n)
        run(lambda: flat_check(n))
        run(lambda: beat("tradebot", quote_ok=LAST_QUOTE_OK))
        if market_open(n):
            if time.time() - last_pos >= POLL_SEC:
                last_pos = time.time()
                run(check_positions)
            if time.time() - last_live >= 60:
                last_live = time.time()
                run(live_watch)
            if can_trade(n) and time.time() - last_nvda >= NVDA_POLL_MIN * 60:
                last_nvda = time.time()
                try:
                    s = nvda_state()
                    if nvda_zone is False and s["zone"]:
                        alert(nvda_text(s), title="NVDA: ready to buy",
                              priority=PRI_ACTION, tags="chart_with_downwards_trend")
                    nvda_zone = s["zone"]
                except Exception:
                    traceback.print_exc()
        time.sleep(20)


if __name__ == "__main__":
    main()
