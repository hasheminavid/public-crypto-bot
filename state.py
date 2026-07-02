"""Atomic JSON state for the crypto bot. Point STATE_DIR at a volume."""
import os
import json
from datetime import datetime, timezone
import config

STATE_FILE = os.path.join(config.STATE_DIR, "crypto_bot_state.json")


def load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"positions": {}, "trades": [], "pending_entries": {},
                "day": None, "anchor": None, "halted": False}


def save(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def record_entry(s, symbol, qty, entry, stop, take_profit, stop_order_id=None):
    s["positions"][symbol] = {"qty": qty, "entry": entry, "stop": stop,
                              "take_profit": take_profit, "opened": now_iso(),
                              "stop_order_id": stop_order_id}
    s.setdefault("trades", []).append(
        {"t": now_iso(), "side": "BUY", "symbol": symbol, "qty": qty, "price": entry})
    save(s)


def record_exit(s, symbol, price, reason):
    pos = s["positions"].pop(symbol, None)
    if pos:
        pnl = (price - pos["entry"]) * pos["qty"]
        s.setdefault("trades", []).append(
            {"t": now_iso(), "side": "SELL", "symbol": symbol, "qty": pos["qty"],
             "price": price, "reason": reason, "pnl": round(pnl, 2)})
    save(s)
