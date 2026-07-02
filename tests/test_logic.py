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

# 4. $ cap sizing
qty, sd = bot._size(equity=100000, atr_val=10, price=100, buying_power=100000)
check("4a MAX_POSITION_NOTIONAL caps size",
      qty * 100 <= config.MAX_POSITION_NOTIONAL + 1e-6)

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

print()
sys.exit(1 if fails else 0)
