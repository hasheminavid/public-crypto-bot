"""Config for the Public.com CRYPTO mean-reversion bot (small study account).
Everything from env / .env. No secrets hard-coded. REAL MONEY when DRY_RUN=false.
"""
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --- Public.com credentials ---
PUBLIC_API_SECRET = os.environ.get("PUBLIC_API_SECRET", "")
PUBLIC_ACCOUNT    = os.environ.get("PUBLIC_ACCOUNT", "")

# --- SAFETY ---
# DRY_RUN=true (default) NEVER sends an order. Flip to false only when YOU
# have decided to go live. The agent never flips this.
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

# --- universe: liquid majors only (daily UTC bars, 24/7 market) ---
SYMBOLS = [s.strip().upper() for s in
           os.environ.get("SYMBOLS", "BTC,ETH").split(",") if s.strip()]

# --- strategy (RSI(2) mean reversion, long-only — same tested family as the
#     equity bot; on crypto it is an EXPERIMENT, not a proven edge) ---
MR_RSI_PERIOD = int(os.environ.get("MR_RSI_PERIOD", "2"))
MR_RSI_BUY    = float(os.environ.get("MR_RSI_BUY", "10"))
MR_TREND_SMA  = int(os.environ.get("MR_TREND_SMA", "200"))
MR_EXIT_SMA   = int(os.environ.get("MR_EXIT_SMA", "5"))
ATR_PERIOD    = int(os.environ.get("ATR_PERIOD", "14"))

# --- risk / sizing (crypto = wider stops, smaller risk, hard $ cap) ---
RISK_PERCENT   = float(os.environ.get("RISK_PERCENT", "0.005"))   # 0.5%/trade
SL_ATR_MULT    = float(os.environ.get("SL_ATR_MULT", "3.0"))      # wide for crypto
RR             = float(os.environ.get("RR", "1.5"))
MAX_POSITIONS  = int(os.environ.get("MAX_POSITIONS", "2"))
MAX_DAILY_LOSS = float(os.environ.get("MAX_DAILY_LOSS", "0.03"))
PAPER_BALANCE  = float(os.environ.get("PAPER_BALANCE", "1000"))
# absolute cap per position — this is a small STUDY account
MAX_POSITION_NOTIONAL = float(os.environ.get("MAX_POSITION_NOTIONAL", "100"))
MIN_NOTIONAL   = float(os.environ.get("MIN_NOTIONAL", "2"))
QTY_PRECISION  = int(os.environ.get("QTY_PRECISION", "8"))        # crypto fractional

# --- runtime (24/7 market: poll fast enough for the bot-side stop) ---
POLL_SECONDS   = int(os.environ.get("POLL_SECONDS", "300"))       # 5 min
STATE_DIR      = os.environ.get("STATE_DIR", ".")

# --- protective stop time-in-force (broker stop attempted first; if Public
#     rejects STOP for crypto, the bot enforces the stop on every poll) ---
STOP_GTD_DAYS  = int(os.environ.get("STOP_GTD_DAYS", "30"))

# --- Telegram alerts ---
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT", "")

# --- review-loop discipline ---
MIN_TRADES_FOR_TUNING = int(os.environ.get("MIN_TRADES_FOR_TUNING", "30"))

# --- hard bounds for the daily Claude review ---
HARD_BOUNDS = {
    "MR_RSI_BUY":   (5.0, 15.0),
    "SL_ATR_MULT":  (2.0, 4.0),
    "RISK_PERCENT": (0.001, 0.0075),
    "MAX_POSITIONS": (1, 3),
    "MR_EXIT_SMA":  (3, 10),
}

# --- apply bounded overrides written by the daily review (params.json) ---
def _apply_review_overrides():
    import json as _json
    try:
        with open(os.path.join(STATE_DIR, "params.json")) as _f:
            _p = _json.load(_f)
    except Exception:
        return
    g = globals()
    for _k, _v in (_p.get("params") or {}).items():
        if _k in HARD_BOUNDS and _k in g:
            _lo, _hi = HARD_BOUNDS[_k]
            try:
                _v = type(g[_k])(_v)
            except (TypeError, ValueError):
                continue
            g[_k] = min(max(_v, _lo), _hi)
_apply_review_overrides()
