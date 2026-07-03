"""Trend-following (momentum) signal on daily bars, long-only.

Better matched to crypto's trending, fat-tailed behaviour than mean reversion:
crypto's big returns come in sustained trends, and "oversold" dips often keep
falling. This rides trends instead of fading dips. It is an EXPERIMENT, NOT a
proven edge — trend strategies are regime-dependent and whipsaw in choppy
markets (see the project's BACKTEST_FINDINGS). Kept deliberately low-parameter
to resist overfitting.

Entry : close > SMA(TREND_SMA)  AND  close > close[MOM_LOOKBACK bars ago]
        (price in an uptrend AND positive medium-term momentum)
Exit  : close < SMA(TREND_SMA)                              (trend break)
The protective ATR stop (managed + trailed in bot.py) is the disaster backstop.
"""
import numpy as np
import config
from indicators import sma, atr


def evaluate(bars):
    """bars = [(ts,o,h,l,c),...]. Returns dict with action in
    {'BUY','EXIT','FLAT_NO_SETUP','SKIP'} plus context, using the latest bar."""
    need = config.TREND_SMA + config.MOM_LOOKBACK + 2
    if len(bars) < need:
        return {"action": "SKIP", "reason": "not enough history"}
    c = [b[4] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]
    trend = sma(c, config.TREND_SMA)
    a = atr(h, l, c, config.ATR_PERIOD)
    last = c[-1]
    mom_ref = c[-1 - config.MOM_LOOKBACK]
    ctx = {"close": last, "sma_trend": float(trend[-1]),
           "momentum": float(last - mom_ref), "atr": float(a[-1])}
    valid = not np.isnan(trend[-1])
    if valid and last > trend[-1] and last > mom_ref and a[-1] > 0:
        ctx["action"] = "BUY"                 # uptrend + positive momentum
    elif valid and last < trend[-1]:
        ctx["action"] = "EXIT"                # trend break
    else:
        ctx["action"] = "FLAT_NO_SETUP"
    return ctx
