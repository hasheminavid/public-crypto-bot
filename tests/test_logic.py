"""Offline logic tests, mocked broker — no network, no orders.
Covers: reconciliation/no-double-sell, pending fills + stops off real fill,
bot-enforced live-quote stop, $ cap sizing, breaker, cancel-before-exit."""
import os, sys, tempfile

tmp = tempfile.mkdtemp()
os.environ.update({"STATE_DIR": tmp, "PUBLIC_API_SECRET": "test-key",
                   "DRY_RUN": "false", "SYMBOLS": "BTC,ETH", "TG_TOKEN": "", "TG_CHAT": ""})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import state as st
import bot
import broker
import strategy

CALLS = []
PRICES = {}

def fake_bars(sym, min_bars=260, include_forming=False):
    return [(f"2026-01-{i%28+1:02d}", 100, 101, 99, 100) for i in range(300)]

def make_broker(positions=None, orders=None, order_states=None, prices=None):
    CALLS.clear(); PRICES.clear(); PRICES.update(prices or {})
    broker.daily_bars = fake_bars
    broker.intraday_bars = lambda sym, hours=4: [("t", 100, 101, 99, PRICES.get(sym, 100.0))]
    broker.last_price = lambda sym: PRICES.get(sym, 100.0)
    broker.positions = lambda: dict(positions or {})
    broker.open_orders = lambda: list(orders or [])
    broker.account_summary = lambda: {"equity": 1000.0, "buying_power": 1000.0, "source": "public"}
    broker.order_status = lambda oid: (order_states or {}).get(oid, {"status": "NEW"})
    broker.cancel_order = lambda oid: CALLS.append(("cancel", oid)) or True
    broker.market_sell = lambda sym, q, px, reason="": CALLS.append(("sell", sym, q, reason)) or {"order_id": "s1"}
    broker.market_buy = lambda sym, q, px: CALLS.append(("buy", sym, q)) or {"order_id": "b1"}
    broker.place_protective_stop = lambda sym, q, sp: CALLS.append(("stop", sym, q, round(sp, 2))) or {"order_id": "stop1"}

def fresh_state(**kw):
    s = {"positions": {}, "trades": [], "pending_entries": {}, "day": None,
         "anchor": None, "halted": False}
    s.update(kw); st.save(s); return s

fails = 0
def check(name, cond):
    global fails
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond: fails += 1

# 1. broker-side close -> reconcile, no double sell
fresh_state(positions={"BTC": {"qty": 0.001, "entry": 100000, "stop": 95000,
                               "take_profit": 110000, "stop_order_id": "old"}})
make_broker(positions={}, orders=[{"order_id": "old", "symbol": "BTC", "side": "SELL",
                                   "type": "STOP", "status": "NEW", "stop_price": 95000.0,
                                   "quantity": 0.001}])
s = st.load(); bot.reconcile(s); s = st.load()
check("1a position reconciled out", "BTC" not in s["positions"])
check("1b orphan cancelled, no sell", ("cancel", "old") in CALLS
      and not any(c[0] == "sell" for c in CALLS))

# 2. bot-enforced live-quote stop (no broker stop resting)
fresh_state(positions={"BTC": {"qty": 0.001, "entry": 100000, "stop": 95000,
                               "take_profit": 110000, "stop_order_id": None}})
make_broker(positions={"BTC": 0.001}, prices={"BTC": 94000.0})
s = st.load(); bot.check_bot_stops(s); s = st.load()
check("2a live-quote stop fired", any(c[0] == "sell" for c in CALLS))
check("2b position closed locally", "BTC" not in s["positions"])
# price above stop -> no action
fresh_state(positions={"BTC": {"qty": 0.001, "entry": 100000, "stop": 95000,
                               "take_profit": 110000, "stop_order_id": None}})
make_broker(positions={"BTC": 0.001}, prices={"BTC": 99000.0})
s = st.load(); bot.check_bot_stops(s)
check("2c no stop above level", not any(c[0] == "sell" for c in CALLS))

# 3. pending fill -> stop off real fill
fresh_state(pending_entries={"b9": {"symbol": "ETH", "qty": 0.05, "signal_close": 2000.0,
                                    "stop_dist": 100.0, "t": "t"}})
make_broker(order_states={"b9": {"status": "FILLED", "filled_quantity": 0.05,
                                 "average_price": 2010.0}})
s = st.load(); bot.process_pending(s); s = st.load()
check("3a stop = fill - dist = 1910", ("stop", "ETH", 0.05, 1910.0) in CALLS)
check("3b entry recorded at real fill", s["positions"]["ETH"]["entry"] == 2010.0)

# 4. position cap sizing: min($100 abs, 40% equity)
# tight ATR would want a huge position; cap must bind.
qty, sd = bot._size(equity=100, atr_val=0.01, price=100, buying_power=100)
notional = qty * 100
check("4a position capped to 40% of $100 equity ($40)",
      notional <= 0.40 * 100 + 1e-6 and notional > 0)
# on a large account the absolute $100 cap binds instead
qty2, _ = bot._size(equity=100000, atr_val=0.01, price=100, buying_power=100000)
check("4b absolute $100 notional cap still binds on big account",
      qty2 * 100 <= config.MAX_POSITION_NOTIONAL + 1e-6)

# 5. breaker
fresh_state(day=st.today_utc(), anchor=1100.0, anchor_source="public")
make_broker()
s = st.load()
check("5a breaker trips", bot._breaker_ok(s, 1000.0, "public") is False
      and st.load()["halted"])

# 6. mean-touch exit cancels stop first
fresh_state(positions={"BTC": {"qty": 0.001, "entry": 100000, "stop": 95000,
                               "take_profit": 110000, "stop_order_id": "stopA"}})
make_broker(positions={"BTC": 0.001},
            orders=[{"order_id": "stopA", "symbol": "BTC", "side": "SELL", "type": "STOP",
                     "status": "NEW", "stop_price": 95000.0, "quantity": 0.001}],
            prices={"BTC": 101000.0})
orig = strategy.evaluate
strategy.evaluate = lambda bars: {"action": "EXIT", "close": 101000.0, "rsi": 80, "atr": 500.0}
bot.run_once(force=True)
order = [c[0] for c in CALLS if c[0] in ("cancel", "sell")]
check("6a cancel before sell", order[:2] == ["cancel", "sell"])
strategy.evaluate = orig

# 7. trend-following evaluate: uptrend+momentum -> BUY; below trend -> EXIT
import strategy as _strat
# rising series: last close well above SMA100 and above 30-bars-ago
rising = [(f"d{i}", 100+i, 100+i, 100+i, 100.0+i) for i in range(200)]
res_up = _strat.evaluate(rising)
check("7a rising trend -> BUY", res_up.get("action") == "BUY" and res_up["momentum"] > 0)
# falling series: last close below SMA100
falling = [(f"d{i}", 300-i, 300-i, 300-i, 300.0-i) for i in range(200)]
res_dn = _strat.evaluate(falling)
check("7b downtrend -> EXIT", res_dn.get("action") == "EXIT")

# 8. trailing stop ratchets UP but never down (via a live run_once daily pass)
fresh_state(positions={"BTC": {"qty": 0.001, "entry": 100000, "stop": 90000,
                               "take_profit": 110000, "stop_order_id": None}})
make_broker(positions={"BTC": 0.001}, prices={"BTC": 130000.0})
# price high, in-trend (no exit): evaluate returns FLAT_NO_SETUP w/ close & atr
_strat_eval = strategy.evaluate
strategy.evaluate = lambda bars: {"action": "FLAT_NO_SETUP", "close": 130000.0,
                                  "atr": 2000.0, "momentum": 5000.0}
import config as _cfg
bot.run_once(force=True)
new_stop = st.load()["positions"]["BTC"]["stop"]
check("8a stop ratcheted up (130000 - 3*2000 = 124000)", abs(new_stop - 124000.0) < 1e-6)
# now a lower price must NOT lower the stop
make_broker(positions={"BTC": 0.001}, prices={"BTC": 125000.0})
s2 = st.load(); s2["last_bar"] = None; st.save(s2)
strategy.evaluate = lambda bars: {"action": "FLAT_NO_SETUP", "close": 125000.0,
                                  "atr": 2000.0, "momentum": 3000.0}
bot.run_once(force=True)
check("8b stop never trails down", st.load()["positions"]["BTC"]["stop"] == new_stop)
strategy.evaluate = _strat_eval

# 9. intraday 4h-resample: two 4h blocks from 1h bars, forming block dropped
import broker as _bk, datetime as _dtm
now = _dtm.datetime.now(_dtm.timezone.utc)
# build 1h bars spanning an already-completed 4h block (block A) fully in the past
base = now.replace(minute=0, second=0, microsecond=0) - _dtm.timedelta(hours=12)
base = base.replace(hour=(base.hour // 4) * 4)   # align to a 4h boundary
raw = []
for i in range(4):   # 4 one-hour bars => one complete 4h candle
    ts = (base + _dtm.timedelta(hours=i)).isoformat().replace("+00:00", "Z")
    raw.append((ts, 10.0+i, 20.0+i, 5.0+i, 12.0+i))
res = _bk._resample(raw, 4)
check("9a resample makes 1 candle", len(res) == 1)
check("9b OHLC = open10 high(max23) low(min5) close(last15)",
      res[0][1] == 10.0 and res[0][2] == 23.0 and res[0][3] == 5.0 and res[0][4] == 15.0)
# current forming block must be dropped
cur = [(now.isoformat().replace("+00:00","Z"), 1,2,0.5,1.5)]
check("9c forming block dropped", _bk._resample(cur, 4) == [])

# 10. intraday trend-break exit: 4h close below trend-buffer -> exit; above -> hold
import strategy as _st
_orig = strategy.evaluate
strategy.evaluate = lambda bars: {"action": "FLAT_NO_SETUP", "close": 100000.0,
                                  "sma_trend": 100000.0, "atr": 2000.0, "momentum": 1.0}
# below: trend 100000, buffer 0.25*2000=500 => threshold 99500; 98000 < 99500 -> exit
fresh_state(positions={"BTC": {"qty": 0.001, "entry": 100000, "stop": 95000,
                               "take_profit": 110000, "stop_order_id": "stopX"}})
make_broker(positions={"BTC": 0.001},
            orders=[{"order_id": "stopX", "symbol": "BTC", "side": "SELL", "type": "STOP",
                     "status": "NEW", "stop_price": 95000.0, "quantity": 0.001}])
broker.intraday_bars = lambda sym, hours=4: [("t", 99000, 99000, 98000, 98000.0)]
s = st.load(); bot.check_intraday_trend_exit(s); s = st.load()
check("10a intraday break exits (cancel then sell)",
      ("cancel", "stopX") in CALLS and any(c[0] == "sell" for c in CALLS)
      and "BTC" not in s["positions"])
# above threshold -> no action
fresh_state(positions={"BTC": {"qty": 0.001, "entry": 100000, "stop": 95000,
                               "take_profit": 110000, "stop_order_id": None}})
make_broker(positions={"BTC": 0.001})
broker.intraday_bars = lambda sym, hours=4: [("t", 100500, 101000, 100000, 100200.0)]
s = st.load(); bot.check_intraday_trend_exit(s)
check("10b 4h close above trend -> no exit", not any(c[0] == "sell" for c in CALLS))
strategy.evaluate = _orig

print()
sys.exit(1 if fails else 0)
