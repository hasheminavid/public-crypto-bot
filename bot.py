"""Public.com CRYPTO mean-reversion bot — small STUDY account.

Deterministic loop (no LLM in the trading path), 24/7 market, 5-min poll:
  every cycle : reconcile vs broker truth -> confirm pending fills -> breaker
                -> live-quote stop check for positions without a broker stop
  once per UTC day (new completed daily bar):
                mean-touch exits -> new risk-sized entries + protective stops

DRY_RUN=true by default. Hard $ cap per position (MAX_POSITION_NOTIONAL).

    python bot.py            # run forever
    python bot.py --once     # single pass (forces the daily pass; testing)
"""
import sys
import time
import math
import traceback

import config
import broker
import strategy
import state as st
from alerts import log, alert

TERMINAL = ("REJECTED", "CANCELLED", "QUEUED_CANCELLED", "EXPIRED", "REPLACED", "UNKNOWN")


def _round_qty(q):
    f = 10 ** config.QTY_PRECISION
    return math.floor(q * f) / f


def _size(equity, atr_val, price, buying_power):
    """Risk-based size, capped by buying power AND the hard $ cap."""
    stop_dist = config.SL_ATR_MULT * atr_val
    if stop_dist <= 0 or price <= 0:
        return 0.0, 0.0
    qty = (equity * config.RISK_PERCENT) / stop_dist
    notional_cap = min(config.MAX_POSITION_NOTIONAL,
                       config.MAX_POSITION_PCT * equity)   # % of equity guard
    qty = min(qty, (buying_power * 0.98) / price, notional_cap / price)
    return _round_qty(qty), stop_dist


# --------------- reconciliation (live only) ---------------
def reconcile(s):
    if config.DRY_RUN or not broker.has_key():
        return
    try:
        held = broker.positions() or {}
        working = broker.open_orders()
    except Exception as e:
        n = s.get("broker_fail_streak", 0) + 1
        s["broker_fail_streak"] = n; st.save(s)
        if n == 5:
            alert(f"ACTION NEEDED — Public API unreachable {n} cycles in a row ({e}). "
                  f"Check PUBLIC_API_SECRET in Railway -> public-crypto-bot -> Variables.")
        else:
            log(f"reconcile skipped (broker read failed: {e})")
        return
    if s.get("broker_fail_streak"):
        s["broker_fail_streak"] = 0; st.save(s)

    by_sym = {}
    for o in working:
        by_sym.setdefault(o["symbol"], []).append(o)

    for sym in list(s["positions"]):
        pos = s["positions"][sym]
        qty_at_broker = held.get(sym, 0.0)
        if qty_at_broker <= 0:
            for o in by_sym.get(sym, []):
                try:
                    broker.cancel_order(o["order_id"])
                except Exception as e:
                    alert(f"{sym}: failed to cancel orphaned order {o['order_id']}: {e}")
            px = broker.last_price(sym) or pos.get("stop") or pos["entry"]
            st.record_exit(s, sym, px, "broker_reconcile(stop_fired_or_manual)")
            alert(f"{sym}: position closed at broker -> state reconciled, "
                  f"orphaned orders cancelled")
        elif qty_at_broker < pos["qty"] * 0.999:
            alert(f"{sym}: broker holds {qty_at_broker} vs local {pos['qty']} "
                  f"-> adopting broker quantity")
            pos["qty"] = qty_at_broker
            st.save(s)

    for sym, pos in s["positions"].items():
        if held.get(sym, 0.0) <= 0:
            continue
        stop_resting = any(o["type"] == "STOP" and o["side"] == "SELL"
                           for o in by_sym.get(sym, []))
        if not stop_resting and pos.get("stop") and pos.get("stop_order_id"):
            try:
                r = broker.place_protective_stop(sym, pos["qty"], pos["stop"])
                pos["stop_order_id"] = r.get("order_id")
                st.save(s)
                alert(f"{sym}: protective stop was missing -> re-placed @ {pos['stop']:.2f}")
            except Exception as e:
                pos["stop_order_id"] = None
                st.save(s)
                alert(f"{sym}: could not re-place broker stop ({e}); switching to "
                      f"bot-enforced stop (checked every {config.POLL_SECONDS}s)")


# --------------- pending entry orders ---------------
def process_pending(s):
    pending = s.get("pending_entries", {})
    for oid in list(pending):
        pe = pending[oid]
        sym = pe["symbol"]
        try:
            o = broker.order_status(oid)
        except Exception as e:
            log(f"{sym}: pending order {oid} status check failed ({e}); retry")
            continue
        status = o["status"]
        if status == "FILLED":
            fill = o.get("average_price") or pe["signal_close"]
            qty = o.get("filled_quantity") or pe["qty"]
            stop = fill - pe["stop_dist"]
            tp = fill + config.RR * pe["stop_dist"]
            stop_id = None
            try:
                r = broker.place_protective_stop(sym, qty, stop)
                stop_id = r.get("order_id")
            except Exception as e:
                alert(f"{sym}: broker STOP not accepted ({e}) — bot enforces the "
                      f"stop on every {config.POLL_SECONDS}s poll instead")
            st.record_entry(s, sym, qty, fill, stop, tp, stop_order_id=stop_id)
            del pending[oid]
            st.save(s)
            alert(f"{sym}: FILLED {qty} @ {fill:.2f} | stop {stop:.2f} "
                  f"({'broker GTD' if stop_id else 'bot-enforced'}) | tp {tp:.2f}")
        elif status in TERMINAL:
            del pending[oid]
            st.save(s)
            alert(f"{sym}: entry order {status} -> dropped")
        else:
            log(f"{sym}: entry order still {status}; waiting")


# --------------- circuit breaker ---------------
def _breaker_ok(s, equity, source):
    day = st.today_utc()
    stale = bool(s.get("anchor")) and equity < s["anchor"] * 0.5
    if s.get("day") != day or s.get("anchor_source") != source or stale:
        s["day"] = day; s["anchor"] = equity; s["anchor_source"] = source
        s["halted"] = False
        st.save(s)
    if s.get("halted"):
        return False
    anchor = s.get("anchor") or equity
    dd = (anchor - equity) / anchor if anchor else 0.0
    if dd >= config.MAX_DAILY_LOSS:
        s["halted"] = True; st.save(s)
        alert(f"CIRCUIT BREAKER: {dd:.2%} daily drawdown -> halting new entries today")
        return False
    return True


# --------------- intraday bot-enforced stop (24/7) ---------------
def check_bot_stops(s):
    """For positions WITHOUT a broker stop: live-quote check every poll."""
    for sym in list(s["positions"]):
        pos = s["positions"][sym]
        if pos.get("stop_order_id"):
            continue
        try:
            px = broker.last_price(sym)
        except Exception as e:
            log(f"{sym}: stop check skipped ({e})")
            continue
        if px is not None and px <= pos["stop"]:
            broker.market_sell(sym, pos["qty"], px, reason="protective stop (bot, live quote)")
            st.record_exit(s, sym, px, "stop")
            alert(f"{sym}: bot-enforced stop exit {pos['qty']} @~{px:.2f}")


# --------------- intraday trend-break exit (24/7: faster than daily close) ---
def check_intraday_trend_exit(s):
    """If a held position's latest completed INTRADAY_TF_HOURS candle closes
    below the daily trend line (minus INTRADAY_BUFFER_ATR*ATR), exit now rather
    than waiting for the daily close. Asymmetric: entries stay slow/daily; exits
    can fire intraday. Never raises."""
    if not config.INTRADAY_EXIT or not s.get("positions"):
        return
    for sym in list(s["positions"]):
        pos = s["positions"][sym]
        try:
            r = strategy.evaluate(broker.daily_bars(sym))
            trend = r.get("sma_trend"); atr_now = r.get("atr") or 0.0
            if trend is None or (isinstance(trend, float) and trend != trend):
                continue
            ib = broker.intraday_bars(sym, config.INTRADAY_TF_HOURS)
            if not ib:
                continue
            last_close = ib[-1][4]
            if last_close < trend - config.INTRADAY_BUFFER_ATR * atr_now:
                if pos.get("stop_order_id"):
                    try:
                        broker.cancel_order(pos["stop_order_id"])
                    except Exception as e:
                        alert(f"{sym}: intraday exit — stop cancel failed ({e}); "
                              f"NOT selling, retry next poll")
                        continue
                broker.market_sell(sym, pos["qty"], last_close,
                                   reason=f"intraday trend break "
                                          f"({config.INTRADAY_TF_HOURS}h close < SMA)")
                st.record_exit(s, sym, last_close, "intraday_trend_break")
                alert(f"{sym}: intraday trend-break exit {pos['qty']} @~{last_close:.2f} "
                      f"({config.INTRADAY_TF_HOURS}h close {last_close:.2f} < "
                      f"trend {trend:.2f})")
        except Exception as e:
            log(f"{sym}: intraday exit check skipped ({e})")


# --------------- main cycle ---------------
def run_once(force=False):
    s = st.load()

    reconcile(s)
    process_pending(s)
    acct = broker.account_summary()
    equity = acct["equity"]; bp = acct["buying_power"]
    breaker_ok = _breaker_ok(s, equity, acct["source"])
    check_bot_stops(s)
    check_intraday_trend_exit(s)  # faster, softer trend-break exit

    # daily pass: once per new completed UTC bar
    ref = broker.daily_bars(config.SYMBOLS[0])
    if not ref:
        log("no data"); return
    last_bar = str(ref[-1][0])
    if not force and s.get("last_bar") == last_bar:
        return
    s["last_bar"] = last_bar; st.save(s)
    log(f"daily pass | equity ${equity:,.2f} ({acct['source']}) | "
        f"holdings {list(s['positions'])} | breaker {'OK' if breaker_ok else 'HALTED'}")

    # exits (trend break on completed daily bar) + trailing-stop ratchet
    for sym in list(s["positions"]):
        pos = s["positions"][sym]
        r = strategy.evaluate(broker.daily_bars(sym))
        close = r.get("close")
        if close is None:
            continue
        if r["action"] == "EXIT":
            if pos.get("stop_order_id"):
                try:
                    broker.cancel_order(pos["stop_order_id"])
                except Exception as e:
                    alert(f"{sym}: cancel of resting stop failed ({e}) — NOT selling "
                          f"to avoid a double-sell; retry next pass.")
                    continue
            broker.market_sell(sym, pos["qty"], close, reason="trend break (close<SMA)")
            st.record_exit(s, sym, close, "trend_break")
            alert(f"{sym}: trend-break exit {pos['qty']} @~{close:.2f}")
            continue
        # still in the trend: ratchet the protective stop UP (never down) so
        # winners are let run but give-back is capped. Trend-following core.
        atr_now = r.get("atr") or 0.0
        if atr_now > 0:
            new_stop = close - config.SL_ATR_MULT * atr_now
            if new_stop > pos.get("stop", 0.0):
                old_stop = pos.get("stop", 0.0)
                pos["stop"] = new_stop
                st.save(s)
                if pos.get("stop_order_id"):     # move the resting broker stop up
                    try:
                        broker.cancel_order(pos["stop_order_id"])
                        rr = broker.place_protective_stop(sym, pos["qty"], new_stop)
                        pos["stop_order_id"] = rr.get("order_id")
                        st.save(s)
                    except Exception as e:
                        pos["stop_order_id"] = None; st.save(s)
                        alert(f"{sym}: trailing-stop re-place failed ({e}); bot "
                              f"enforces at {new_stop:.2f} on every poll")
                log(f"{sym}: trailing stop {old_stop:.2f} -> {new_stop:.2f}")

    # entries
    open_count = len(s["positions"]) + len(s.get("pending_entries", {}))
    for sym in config.SYMBOLS:
        if sym in s["positions"] or sym in {p["symbol"] for p in
                                            s.get("pending_entries", {}).values()}:
            continue
        if not breaker_ok or open_count >= config.MAX_POSITIONS:
            break
        r = strategy.evaluate(bars=broker.daily_bars(sym))
        if r.get("action") != "BUY":
            continue
        entry = r["close"]
        qty, stop_dist = _size(equity, r["atr"], entry, bp)
        notional = qty * entry
        if qty <= 0 or notional < config.MIN_NOTIONAL:
            log(f"{sym}: BUY signal but size too small (${notional:.2f}); skip")
            continue
        log(f"{sym}: BUY setup (trend up, mom {r['momentum']:+.2f}) -> {qty} "
            f"(~${notional:.2f}, cap ${config.MAX_POSITION_NOTIONAL:.0f})")
        res = broker.market_buy(sym, qty, entry)
        s.setdefault("pending_entries", {})[res["order_id"]] = {
            "symbol": sym, "qty": qty, "signal_close": entry,
            "stop_dist": stop_dist, "t": st.now_iso()}
        st.save(s)
        open_count += 1
    process_pending(s)


def main():
    once = "--once" in sys.argv
    alert(f"Public CRYPTO bot starting | {'DRY-RUN' if config.DRY_RUN else 'LIVE'} | "
          f"{config.SYMBOLS} | risk {config.RISK_PERCENT:.2%} | cap "
          f"${config.MAX_POSITION_NOTIONAL:.0f}/pos | max_pos {config.MAX_POSITIONS} | "
          f"breaker {config.MAX_DAILY_LOSS:.1%}")
    if not config.DRY_RUN:
        alert("WARNING: DRY_RUN=false — this places REAL crypto orders with REAL money.")
    try:
        s0 = st.load(); a0 = broker.account_summary()
        if s0.get("anchor_source") != a0["source"] or (
                s0.get("anchor") and a0["equity"] < s0["anchor"] * 0.5):
            s0.update({"day": st.today_utc(), "anchor": a0["equity"],
                       "anchor_source": a0["source"], "halted": False})
            st.save(s0)
            log(f"reconciled state -> {a0['source']} equity ${a0['equity']:,.2f}")
    except Exception as e:
        log(f"startup reconcile skipped: {e}")
    if once:
        run_once(force=True); return
    import claude_review
    while True:
        try:
            run_once()
            claude_review.maybe_run()
        except Exception as e:
            _s = st.load()
            n = _s.get("loop_fail_streak", 0) + 1
            _s["loop_fail_streak"] = n; st.save(_s)
            alert(f"LOOP ERROR: {e}")
            if n == 5:
                alert("ACTION NEEDED — bot failed 5 cycles in a row. Check Railway -> "
                      "public-crypto-bot -> Deployments -> View logs, then redeploy or "
                      "send the log to Claude.")
            traceback.print_exc()
        else:
            _s = st.load()
            if _s.get("loop_fail_streak"):
                _s["loop_fail_streak"] = 0; st.save(_s)
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
