"""24-hour review: reads bot state + account, writes a dated report with realized/
open P&L, win rate, a SPY benchmark, and mechanical improvement flags. A scheduled
Claude routine reads this file and proposes/applies the qualitative refinements.

    python review.py
"""
import os
import time
from datetime import datetime, timezone

import config
import broker
import state as st


def _retry(fn, tries=3, delay=2.0, what="API call"):
    """Call fn(), retrying on transient errors (a one-off broker 401/timeout)
    before giving up. Returns fn() or re-raises the last error after `tries`."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < tries - 1:
                print(f"(review: {what} failed [{e}] — retry {i+1}/{tries-1} "
                      f"in {delay:.0f}s)", flush=True)
                time.sleep(delay)
    raise last


def _last_close(symbol):
    try:
        bars = broker.daily_bars(symbol)
        return bars[-1][4] if bars else None
    except Exception:
        return None


def _btc_return(since_iso):
    """Buy-and-hold SPY return since the first trade date, as a benchmark."""
    try:
        bars = broker.daily_bars("BTC")
        if not bars or not since_iso:
            return None
        since = since_iso[:10]
        past = [b for b in bars if str(b[0])[:10] <= since]
        if not past:
            return None
        return bars[-1][4] / past[-1][4] - 1.0
    except Exception:
        return None


def main():
    s = st.load()
    trades = s.get("trades", [])
    sells = [t for t in trades if t.get("side") == "SELL"]
    realized = sum(t.get("pnl", 0.0) for t in sells)
    wins = [t for t in sells if t.get("pnl", 0) > 0]
    stops = [t for t in sells if t.get("reason") == "stop"]
    winrate = (len(wins) / len(sells) * 100) if sells else 0.0

    # --- intraday (4h) exit monitoring ---
    intraday = [t for t in sells if t.get("reason") == "intraday_trend_break"]
    trendbreak = [t for t in sells if t.get("reason") == "trend_break"]
    intr_wins = [t for t in intraday if t.get("pnl", 0) > 0]
    intr_wr = (len(intr_wins) / len(intraday) * 100) if intraday else 0.0
    intr_pnl = sum(t.get("pnl", 0.0) for t in intraday)
    # whipsaw = an intraday exit that was RE-ENTERED (same symbol) within 5 days
    # -> strong evidence the 4h break was false and the exit fired too eagerly.
    import datetime as _d
    def _pt(x):
        try:
            return _d.datetime.fromisoformat(x)
        except Exception:
            return None
    whips = 0
    for i, t in enumerate(trades):
        if t.get("side") == "SELL" and t.get("reason") == "intraday_trend_break":
            et = _pt(t.get("t", "")); sym0 = t.get("symbol")
            for u in trades[i + 1:]:
                if u.get("side") == "BUY" and u.get("symbol") == sym0:
                    bt = _pt(u.get("t", ""))
                    if et and bt and 0 <= (bt - et).days <= 5:
                        whips += 1
                    break

    acct = _retry(broker.account_summary, what="account_summary")
    open_lines, unreal = [], 0.0
    for sym, p in s.get("positions", {}).items():
        px = _last_close(sym)
        if px is None:
            open_lines.append(f"- {sym}: {p['qty']} @ {p['entry']:.2f} (stop {p['stop']:.2f}) — price n/a")
            continue
        u = (px - p["entry"]) * p["qty"]; unreal += u
        open_lines.append(f"- {sym}: {p['qty']} @ {p['entry']:.2f} -> {px:.2f}  "
                          f"unrealized ${u:+.2f}  (stop {p['stop']:.2f})")

    first = trades[0]["t"] if trades else None
    bench = _btc_return(first)

    # ---- mechanical improvement flags (Claude reviews these + the data) ----
    # DISCIPLINE: no parameter-change suggestion is allowed below
    # MIN_TRADES_FOR_TUNING closed trades. Small samples are noise; tuning on
    # them is curve-fitting (see BACKTEST_FINDINGS in the oanda repo). The
    # expected daily outcome is "no change".
    flags = []
    enough = len(sells) >= config.MIN_TRADES_FOR_TUNING
    if sells and winrate < 40:
        if enough:
            flags.append(f"win rate {winrate:.0f}% over {len(sells)} closed trades; if driven "
                         f"by stop-outs ({len(stops)}/{len(sells)}), consider SL_ATR_MULT "
                         f"{config.SL_ATR_MULT}->{config.SL_ATR_MULT+0.5:.1f} (wider stop). "
                         f"Bounds: SL_ATR_MULT must stay within [2.0, 3.5].")
        else:
            flags.append(f"win rate {winrate:.0f}% but only {len(sells)} closed trades "
                         f"(<{config.MIN_TRADES_FOR_TUNING}) — too few to tune on. HOLD parameters.")
    if not trades:
        flags.append("no trades yet — trend-following only holds when price is above "
                     "SMA(trend) with positive momentum; flat/absent in downtrends "
                     "and chop by design. Do NOT loosen the trend filter to force "
                     "this is normal for weeks at a time. Do NOT loosen the entry to "
                     "manufacture activity; that is how edges die.")
    if enough and intraday:
        share = len(intraday) / len(sells) * 100
        if whips >= max(3, len(intraday) * 0.5):
            flags.append(f"intraday-4h exits WHIPSAWED {whips}/{len(intraday)} times "
                         f"(re-entered within 5d) — firing too eagerly on false breaks. "
                         f"WIDEN INTRADAY_BUFFER_ATR {config.INTRADAY_BUFFER_ATR:.2f}->"
                         f"{min(config.INTRADAY_BUFFER_ATR + 0.25, 1.5):.2f} (bounds [0.0, 1.5]).")
        elif intr_wr < 35 and share > 40 and intr_pnl < 0:
            flags.append(f"intraday-4h exits are {share:.0f}% of exits, {intr_wr:.0f}% win, "
                         f"${intr_pnl:+.2f} — consider WIDENING INTRADAY_BUFFER_ATR "
                         f"{config.INTRADAY_BUFFER_ATR:.2f}->"
                         f"{min(config.INTRADAY_BUFFER_ATR + 0.25, 1.5):.2f} (bounds [0.0, 1.5]).")
        elif whips == 0 and intr_pnl > 0 and intr_wr >= 55:
            flags.append(f"intraday-4h exits protecting cleanly (0 whipsaws, {intr_wr:.0f}% win, "
                         f"${intr_pnl:+.2f}); parameters look right — HOLD.")
    if s.get("halted"):
        flags.append("circuit breaker tripped today; review whether risk% is too high for account size.")
    if bench is not None and realized + unreal < 0 and bench > 0 and enough:
        flags.append(f"strategy is behind buy-and-hold BTC ({bench:+.1%}); expected per testing — "
                     f"consider whether the sleeve is worth its complexity vs holding.")
    if not flags:
        flags.append("no mechanical issues flagged; hold parameters, keep observing.")
    flags.append(f"tuning gate: {len(sells)}/{config.MIN_TRADES_FOR_TUNING} closed trades "
                 f"{'(OPEN — bounded changes allowed)' if enough else '(LOCKED — hold all parameters)'}")

    # ---- write dated report ----
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(os.path.join(config.STATE_DIR, "reviews"), exist_ok=True)
    path = os.path.join(config.STATE_DIR, "reviews", f"review_{day}.md")
    lines = [
        f"# Daily review (CRYPTO) — {day}",
        "",
        f"- mode: {'DRY-RUN' if config.DRY_RUN else 'LIVE'} | data: {acct['source']} | "
        f"equity ${acct['equity']:,.0f}",
        f"- realized P&L: ${realized:+.2f} over {len(sells)} closed trades "
        f"(win rate {winrate:.0f}%, {len(stops)} stopped out)",
        f"- exit mix: {len(intraday)} intraday-4h, {len(trendbreak)} daily trend-break, "
        f"{len(stops)} hard-stop",
        f"- intraday-4h exits: win rate {intr_wr:.0f}%, P&L ${intr_pnl:+.2f}, "
        f"whipsawed back within 5d: {whips}/{len(intraday)} "
        f"(buffer {config.INTRADAY_BUFFER_ATR:.2f}xATR)",
        f"- open positions: {len(s.get('positions', {}))} | unrealized ${unreal:+.2f}",
        f"- combined P&L: ${realized + unreal:+.2f}"
        + (f" | BTC buy-hold since start {bench:+.1%}" if bench is not None else ""),
        "",
        "## open positions",
        *(open_lines or ["- none"]),
        "",
        "## improvement flags (for Claude's 24h review)",
        *[f"- {f}" for f in flags],
    ]
    report = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)
    print(f"\n(written to {path})")


if __name__ == "__main__":
    main()
