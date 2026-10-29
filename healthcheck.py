#!/usr/bin/env python3
"""
healthcheck.py - watchdog for the trading setup. Run every 10 minutes by a systemd timer.
Pings ntfy 'alerts' when something breaks (after seeing it twice in a row, to avoid
false alarms) and quietly when it recovers.
"""
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
NTFY = "http://localhost:2586/alerts"
TZ = ZoneInfo("America/Chicago")
STATE = os.path.join(HERE, "health_state.json")
SERVICES = ["ntfy", "tradebot", "newswatch", "ollama"]


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _hdr(text):
    """Header-safe text: plain ASCII stays as is, anything else is RFC 2047 encoded (ntfy decodes it)."""
    import base64
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def notify(msg, title, priority, tags):
    try:
        requests.post(NTFY, data=msg.encode(), timeout=10,
                      headers={"Title": _hdr(title), "Priority": str(priority), "Tags": tags})
    except Exception as e:
        print(f"[ntfy] {e}")


def mins_since(ts):
    return (time.time() - ts) / 60 if ts else 9999


def checks():
    issues = {}
    n = datetime.now(TZ)
    try:
        from market_extras import is_open_now
        market_open = is_open_now(n)
    except Exception:
        market_open = n.weekday() < 5 and 8 * 60 + 30 <= n.hour * 60 + n.minute <= 15 * 60
    holding = bool(load(os.path.join(HERE, "positions.json"), []))

    for s in SERVICES:
        state = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True).stdout.strip()
        if state != "active":
            issues[f"svc:{s}"] = f"{s} service is {state or 'not running'}"

    tb = load(os.path.join(HERE, "heartbeat_tradebot.json"), {})
    if "svc:tradebot" not in issues and mins_since(tb.get("time")) > 5:
        issues["hb:tradebot"] = f"tradebot is frozen (no heartbeat for {mins_since(tb.get('time')):.0f} min)"
    if market_open and holding and mins_since(tb.get("quote_ok")) > 20:
        issues["quotes"] = ("tradebot can't get option prices - your -20% stop alerts are NOT working. "
                            "Watch your positions in Fidelity.")

    nw = load(os.path.join(HERE, "heartbeat_newswatch.json"), {})
    if "svc:newswatch" not in issues and mins_since(nw.get("time")) > 30:
        issues["hb:newswatch"] = f"news scanner is frozen (no heartbeat for {mins_since(nw.get('time')):.0f} min)"
    busy = n.weekday() < 5 and 7 <= n.hour < 20
    if busy and "svc:newswatch" not in issues and mins_since(nw.get("scan_ok")) > 60:
        issues["scan"] = f"news scanner hasn't finished a scan in {mins_since(nw.get('scan_ok')):.0f} min"
    total, failed = nw.get("feeds_total") or 0, nw.get("feeds_failed") or 0
    if total and failed >= max(3, total / 2):
        issues["feeds"] = f"{failed} of {total} news feeds are failing (blocked or down?)"

    try:
        requests.get("http://localhost:11434/api/tags", timeout=5).raise_for_status()
    except Exception:
        if "svc:ollama" not in issues:
            issues["ollama"] = "Ollama isn't answering - headlines can't be read"

    du = shutil.disk_usage(HERE)
    if du.free / du.total < 0.05:
        issues["disk"] = f"disk almost full ({du.free / 1e9:.0f} GB free)"
    return issues, market_open and holding


def main():
    issues, critical_time = checks()
    st = load(STATE, {})
    new_state = {}
    for key, text in issues.items():
        prev = st.get(key)
        entry = {"text": text, "first": prev["first"] if prev else time.time(),
                 "alerted": prev.get("alerted", False) if prev else False}
        if prev and not entry["alerted"]:
            urgent = critical_time and key in ("quotes", "svc:tradebot", "hb:tradebot", "svc:ntfy")
            notify(text + "\n\nCheck on the server: sudo journalctl -u tradebot -u newswatch -n 30",
                   title="\U0001F6E0 Trading system problem", priority=5 if urgent else 4, tags="warning")
            entry["alerted"] = True
        new_state[key] = entry
    for key, prev in st.items():
        if key not in issues and prev.get("alerted"):
            notify(f"Fixed: {prev['text']}", title="✅ Trading system recovered", priority=2,
                   tags="white_check_mark")
    with open(STATE, "w") as f:
        json.dump(new_state, f, indent=2)
    print(f"{datetime.now(TZ):%Y-%m-%d %H:%M} issues: {list(issues) or 'none'}")


if __name__ == "__main__":
    main()
