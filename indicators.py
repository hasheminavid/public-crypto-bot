"""Same indicator math as the backtested strategy (Wilder RSI/ATR, SMA)."""
import numpy as np


def rsi(close, n=2):
    c = np.asarray(close, float)
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    ru = np.zeros(len(c)); rd = np.zeros(len(c))
    if len(c) <= n:
        return np.full(len(c), 50.0)
    ru[n] = up[:n].mean(); rd[n] = dn[:n].mean()
    for i in range(n + 1, len(c)):
        ru[i] = (ru[i - 1] * (n - 1) + up[i - 1]) / n
        rd[i] = (rd[i - 1] * (n - 1) + dn[i - 1]) / n
    rs = np.divide(ru, rd, out=np.full_like(ru, np.inf), where=rd > 0)
    out = 100.0 - 100.0 / (1.0 + rs)
    out[:n] = 50.0
    return out


def sma(x, n):
    x = np.asarray(x, float)
    if len(x) < n:
        return np.full(len(x), np.nan)
    out = np.full(len(x), np.nan)
    cs = np.cumsum(x)
    out[n - 1:] = (cs[n - 1:] - np.concatenate([[0], cs[:-n]])) / n
    return out


def atr(high, low, close, n=14):
    h = np.asarray(high, float); l = np.asarray(low, float); c = np.asarray(close, float)
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    out = np.zeros(len(c))
    if len(c) <= n:
        return out
    out[n] = tr[1:n + 1].mean()
    for i in range(n + 1, len(c)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out
