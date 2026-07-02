"""RSI(2) mean-reversion signal on daily bars (long-only) — the tested strategy.
Entry : Close > SMA(trend) and RSI(period) < buy_thr   (oversold in an uptrend)
Exit  : Close > SMA(exit)                                (snapped back to the mean)
"""
import numpy as np
import config
from indicators import rsi, sma, atr


def evaluate(bars):
    """bars = [(ts,o,h,l,c),...]. Returns dict with action in
    {'BUY','EXIT','FLAT_NO_SETUP'} plus context, using the latest closed bar."""
    if len(bars) < config.MR_TREND_SMA + 3:
        return {"action": "SKIP", "reason": "not enough history"}
    c = [b[4] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]
    r = rsi(c, config.MR_RSI_PERIOD)
    trend = sma(c, config.MR_TREND_SMA)
    exitsma = sma(c, config.MR_EXIT_SMA)
    a = atr(h, l, c, config.ATR_PERIOD)
    last = c[-1]
    ctx = {"close": last, "rsi": float(r[-1]), "sma_trend": float(trend[-1]),
           "sma_exit": float(exitsma[-1]), "atr": float(a[-1])}

    entry = (last > trend[-1]) and (r[-1] < config.MR_RSI_BUY) and not np.isnan(trend[-1])
    exit_ = last > exitsma[-1] if not np.isnan(exitsma[-1]) else False
    if entry and a[-1] > 0:
        ctx["action"] = "BUY"
    elif exit_:
        ctx["action"] = "EXIT"
    else:
        ctx["action"] = "FLAT_NO_SETUP"
    return ctx
