"""Stdout + Telegram alerts (ported from the OANDA bot). Alerting must never
crash the bot. Configure TG_TOKEN / TG_CHAT to receive Telegram messages."""
import urllib.request
import urllib.parse
from datetime import datetime, timezone

import config


def _stamp(msg):
    return f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z  {msg}"


def log(msg):
    """Routine logging -> stdout only (Railway logs)."""
    print(_stamp(msg), flush=True)


def alert(msg):
    """Notable events -> stdout AND Telegram (if configured). Never fatal."""
    line = _stamp(msg)
    print(line, flush=True)
    if not (config.TG_TOKEN and config.TG_CHAT):
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": config.TG_CHAT, "text": f"[public-crypto] {line}"}).encode()
        url = f"https://api.telegram.org/bot{config.TG_TOKEN}/sendMessage"
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        print(f"(alert send failed: {e})", flush=True)
