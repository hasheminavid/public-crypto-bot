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

# --- strategy (TREND-FOLLOWING / momentum, long-only) ---
# Entry: close > SMA(TREND_SMA) and close > close[MOM_LOOKBACK ago]. Exit: close
# < SMA(TREND_SMA). Low-parameter on purpose. EXPERIMENT, not a proven edge.
TREND_SMA     = int(os.environ.get("TREND_SMA", "100"))   # trend filter
MOM_LOOKBACK  = int(os.environ.get("MOM_LOOKBACK", "30"))  # momentum confirmation
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
# no single position may exceed this fraction of live equity (guards a $100
# account: one trade can't swallow the account even if ATR is tight)
MAX_POSITION_PCT = float(os.environ.get("MAX_POSITION_PCT", "0.40"))
MIN_NOTIONAL   = float(os.environ.get("MIN_NOTIONAL", "2"))
QTY_PRECISION  = int(os.environ.get("QTY_PRECISION", "8"))        # crypto fractional

# --- intraday trend-break exit (asymmetric: slow daily entries, fast exits) ---
# Every poll, if a held position's latest completed INTRADAY_TF_HOURS candle
# closes below the daily trend line (minus a small ATR buffer to avoid whipsaw),
# exit now instead of waiting for the daily close. Entries stay daily/confirmed.
INTRADAY_EXIT       = os.environ.get("INTRADAY_EXIT", "true").lower() != "false"
INTRADAY_TF_HOURS   = int(os.environ.get("INTRADAY_TF_HOURS", "4"))    # 4h candles
INTRADAY_BUFFER_ATR = float(os.environ.get("INTRADAY_BUFFER_ATR", "0.25"))

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
    "TREND_SMA":          (50, 200),
    "MOM_LOOKBACK":       (10, 60),
    "SL_ATR_MULT":        (2.0, 5.0),
    "RISK_PERCENT":       (0.001, 0.0075),
    "MAX_POSITIONS":      (1, 3),
    "INTRADAY_BUFFER_ATR": (0.0, 1.5),   # intraday-exit sensitivity (review-tunable)
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
