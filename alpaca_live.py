"""Near-real-time stock prices (Alpaca free IEX feed, Yahoo fallback) and an early-warning watcher."""
import json, os, time, urllib.request, urllib.parse
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
REF_FILE = os.path.join(HERE, "live_ref.json")
DELTA = 0.5
WARN = 0.12
STOPEST = 0.20
REARM = 0.08
REPEAT_MIN = 15
LAST_SOURCE = {"name": "none"}


def _keys():
    k = s = None
    try:
        with open(os.path.join(HERE, "alpaca.env")) as f:
            for line in f:
                if "=" in line:
                    a, b = line.strip().split("=", 1)
                    if a == "ALPACA_KEY":
                        k = b.strip()
                    elif a == "ALPACA_SECRET":
                        s = b.strip()
    except OSError:
        pass
    return k, s


def _alpaca(symbols):
    k, s = _keys()
    if not k or not s:
        raise RuntimeError("no keys in alpaca.env")
    q = urllib.parse.urlencode({"symbols": ",".join(symbols), "feed": "iex"})
    req = urllib.request.Request("https://data.alpaca.markets/v2/stocks/trades/latest?" + q,
                                 headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
    out = {sym: float(v["p"]) for sym, v in data.get("trades", {}).items()}
    if not out:
        raise RuntimeError("empty answer")
    return out


def prices(symbols):
    symbols = sorted(set(symbols))
    out = {}
    if not symbols:
        return out
    try:
        out = _alpaca(symbols)
        LAST_SOURCE["name"] = "Alpaca (live)"
    except Exception as e:
        print(f"[live] alpaca: {e}")
        LAST_SOURCE["name"] = "Yahoo (15 min delayed)"
    missing = [s for s in symbols if s not in out]
    if missing:
        try:
            import yfinance as yf
            for s in missing:
                try:
                    out[s] = float(yf.Ticker(s).fast_info.last_price)
                except Exception as e:
                    print(f"[live] yahoo {s}: {e}")
        except Exception:
            pass
    return out


def _load():
    try:
        with open(REF_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(d):
    tmp = REF_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, REF_FILE)


def estimate(p, ref, price, delta=DELTA):
    move = (price - ref) if p["cp"] == "C" else (ref - price)
    return delta * move / p["entry"] if p["entry"] else 0.0


def watch(positions, alert, n):
    refs = _load()
    ids = {p["id"] for p in positions}
    changed = False
    for k in list(refs):
        if k not in ids:
            refs.pop(k)
            changed = True
    if not positions:
        if changed:
            _save(refs)
        return
    px = prices([p["ticker"] for p in positions])
    for p in positions:
        price = px.get(p["ticker"])
        if not price:
            continue
        r = refs.get(p["id"])
        if not r:
            refs[p["id"]] = {"ref": price, "level": 0, "last": None}
            changed = True
            continue
        try:
            oq = option_quote(p["ticker"], p["expiry"], p["strike"], p["cp"])
        except Exception:
            oq = None
        dlt = abs(oq["delta"]) if oq and oq.get("delta") else DELTA
        est = estimate(p, r["ref"], price, dlt)
        chg = price / r["ref"] - 1
        body = (f"{p['id']}: {p['ticker']} is ${price:.2f}, {chg:+.1%} since you logged it.\n"
                f"Rough option estimate: about {est:+.0%} (stock move x {dlt:.2f}). "
                f"Check the live quote in Fidelity.")
        last = datetime.fromisoformat(r["last"]) if r.get("last") else None
        if est <= -STOPEST:
            if not last or n - last >= timedelta(minutes=REPEAT_MIN):
                alert(body + "\nThis is around your -20% stop. Rule says sell.",
                      title=f"LIVE: {p['ticker']} near your stop", priority=5, tags="rotating_light")
                r["level"], r["last"] = 2, n.isoformat(timespec="seconds")
                changed = True
        elif est <= -WARN and r["level"] < 1:
            alert(body + "\nApproaching your stop. Decide now, not at -20%.",
                  title=f"LIVE: {p['ticker']} moving against you", priority=4, tags="warning")
            r["level"], r["last"] = 1, n.isoformat(timespec="seconds")
            changed = True
        elif est > -REARM and r["level"]:
            r["level"], r["last"] = 0, None
            changed = True
    if changed:
        _save(refs)


def _get(path, params):
    k, s = _keys()
    if not k or not s:
        raise RuntimeError("no keys in alpaca.env")
    req = urllib.request.Request("https://data.alpaca.markets" + path + "?" + urllib.parse.urlencode(params),
                                 headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def trend(symbols):
    """{symbol: {price, day, m15, m60, open}} - changes as fractions, None if unknown."""
    from datetime import timezone
    symbols = sorted(set(symbols))
    joined = ",".join(symbols)
    snaps = _get("/v2/stocks/snapshots", {"symbols": joined, "feed": "iex"})
    start = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        bars = _get("/v2/stocks/bars", {"symbols": joined, "timeframe": "5Min", "start": start,
                                        "feed": "iex", "limit": 1000}).get("bars") or {}
    except Exception as e:
        print(f"[live] bars: {e}")
        bars = {}
    out = {}
    for s in symbols:
        sn = snaps.get(s) or {}
        price = (sn.get("latestTrade") or {}).get("p")
        if not price:
            continue
        price = float(price)
        prev = (sn.get("prevDailyBar") or {}).get("c")
        opn = (sn.get("dailyBar") or {}).get("o")
        bs = bars.get(s) or []

        def back(mins):
            if len(bs) < 2:
                return None
            def ts(b):
                return datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
            target = ts(bs[-1]) - timedelta(minutes=mins)
            older = [b for b in bs if ts(b) <= target]
            return float(older[-1]["c"]) if older else None

        b15, b60 = back(15), back(60)
        out[s] = {"price": price,
                  "day": price / float(prev) - 1 if prev else None,
                  "open": price / float(opn) - 1 if opn else None,
                  "m15": price / b15 - 1 if b15 else None,
                  "m60": price / b60 - 1 if b60 else None}
    return out


def occ(ticker, expiry_iso, strike, cp):
    """Option symbol, like NVDA261023C00225000."""
    y, mo, d = expiry_iso.split("-")
    return f"{ticker}{y[2:]}{mo}{d}{cp}{int(round(float(strike) * 1000)):08d}"


def option_quote(ticker, expiry_iso, strike, cp):
    """{bid, ask, delta, age_min} from Alpaca's option snapshot, or None."""
    from datetime import timezone
    sym = occ(ticker, expiry_iso, strike, cp)
    data = _get("/v1beta1/options/snapshots", {"symbols": sym, "feed": "indicative"})
    sn = (data.get("snapshots") or {}).get(sym)
    if not sn:
        return None
    q = sn.get("latestQuote") or {}
    bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    age = None
    if q.get("t"):
        t = q["t"].replace("Z", "+00:00")
        if "." in t:
            head, rest = t.split(".", 1)
            frac, tz = rest[:-6], rest[-6:]
            t = head + "." + frac[:6] + tz
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(t)).total_seconds() / 60
    return {"bid": bid, "ask": ask, "delta": (sn.get("greeks") or {}).get("delta"), "age_min": age}
