"""Broker adapter for Public.com CRYPTO.

Modes (chosen automatically):
  * LIVE-DATA : PUBLIC_API_SECRET set -> real bars/quotes/positions/orders
  * OFFLINE   : no key -> Yahoo daily bars (SYM-USD), paper balance

Order placement is DRY-RUN by default. Crypto notes:
  * Daily bars are UTC; today's forming bar is dropped unless asked for.
  * A broker-side STOP order is attempted (GTD). If Public rejects STOP for
    crypto, bot.py enforces the stop on every poll using live quotes.
"""
import datetime as _dt
import uuid as _uuid

import config

_client = None


def _drop_forming(out):
    """Drop the last bar if it's today's still-forming UTC session."""
    if not out:
        return out
    try:
        last_date = _dt.date.fromisoformat(str(out[-1][0])[:10])
    except Exception:
        return out
    if last_date == _dt.datetime.now(_dt.timezone.utc).date():
        return out[:-1]
    return out


def _get_client():
    global _client
    if _client is not None:
        return _client
    from public_api_sdk import PublicApiClient, PublicApiClientConfiguration
    from public_api_sdk.auth_config import ApiKeyAuthConfig
    _client = PublicApiClient(
        ApiKeyAuthConfig(api_secret_key=config.PUBLIC_API_SECRET),
        config=PublicApiClientConfiguration(default_account_number=config.PUBLIC_ACCOUNT or None),
    )
    return _client


def has_key():
    return bool(config.PUBLIC_API_SECRET)


# ---------------- market data ----------------
def daily_bars(symbol, min_bars=260, include_forming=False):
    """[(ts, o, h, l, c), ...] daily UTC bars, oldest first."""
    if has_key():
        from public_api_sdk import BarPeriod, BarAggregation, InstrumentType
        resp = _get_client().get_bars(symbol, BarPeriod.FIVE_YEARS,
                                      aggregation=BarAggregation.ONE_DAY,
                                      instrument_type=InstrumentType.CRYPTO)
        bars = resp.regular_market.bars if resp and resp.regular_market else []
        out = [(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close))
               for b in bars]
    else:
        import yfinance as yf
        df = yf.download(f"{symbol}-USD", period="6y", interval="1d",
                         auto_adjust=True, progress=False, threads=False)
        import pandas as pd
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        out = [(ix, float(r.Open), float(r.High), float(r.Low), float(r.Close))
               for ix, r in df.iterrows()]
    return out if include_forming else _drop_forming(out)


def _to_dt(ts):
    """Normalise a bar timestamp (ISO string or datetime/pandas Timestamp) to an
    aware UTC datetime."""
    if isinstance(ts, str):
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    try:
        d = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    except Exception:
        d = _dt.datetime.fromisoformat(str(ts)[:19])
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def _resample(raw, hours):
    """Aggregate 1-hour bars into `hours`-hour UTC candles (OHLC), dropping the
    current still-forming block. raw = [(ts,o,h,l,c),...] oldest first."""
    if not raw:
        return []
    buckets, order = {}, []
    for ts, o, h, l, c in raw:
        d = _to_dt(ts)
        key = (d.year, d.month, d.day, d.hour // hours)
        if key not in buckets:
            buckets[key] = [o, h, l, c]; order.append(key)
        else:
            bk = buckets[key]
            bk[1] = max(bk[1], h); bk[2] = min(bk[2], l); bk[3] = c
    now = _dt.datetime.now(_dt.timezone.utc)
    cur = (now.year, now.month, now.day, now.hour // hours)
    out = []
    for key in order:
        if key == cur:
            continue                     # drop the forming block
        o, h, l, c = buckets[key]
        ts = f"{key[0]:04d}-{key[1]:02d}-{key[2]:02d}T{key[3]*hours:02d}:00Z"
        out.append((ts, o, h, l, c))
    return out


def intraday_bars(symbol, hours=4):
    """`hours`-hour candles (completed only), resampled from 1-hour data. Used by
    the intraday trend-break exit. Public offers 1h/1d only, so 4h is built here."""
    if has_key():
        from public_api_sdk import BarPeriod, BarAggregation, InstrumentType
        resp = _get_client().get_bars(symbol, BarPeriod.MONTH,
                                      aggregation=BarAggregation.ONE_HOUR,
                                      instrument_type=InstrumentType.CRYPTO)
        bars = resp.regular_market.bars if resp and resp.regular_market else []
        raw = [(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close))
               for b in bars]
    else:
        import yfinance as yf
        df = yf.download(f"{symbol}-USD", period="1mo", interval="60m",
                         auto_adjust=True, progress=False, threads=False)
        import pandas as pd
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        raw = [(ix, float(r.Open), float(r.High), float(r.Low), float(r.Close))
               for ix, r in df.iterrows()]
    return _resample(raw, hours)


def last_price(symbol):
    """Live last trade price (None offline — daily close is used instead)."""
    if not has_key():
        bars = daily_bars(symbol, include_forming=True)
        return bars[-1][4] if bars else None
    from public_api_sdk import OrderInstrument, InstrumentType
    q = _get_client().get_quotes(
        [OrderInstrument(symbol=symbol, type=InstrumentType.CRYPTO)])
    if not q or q[0].last is None:
        return None
    return float(q[0].last)


# ---------------- account / positions / orders ----------------
def account_summary():
    if has_key():
        p = _get_client().get_portfolio()
        equity = sum(float(e.value) for e in (p.equity or []))
        bp = getattr(p.buying_power, "cash_only_buying_power", None) or \
             getattr(p.buying_power, "buying_power", None)
        return {"equity": equity, "buying_power": float(bp) if bp is not None else equity,
                "source": "public"}
    return {"equity": config.PAPER_BALANCE, "buying_power": config.PAPER_BALANCE,
            "source": "paper"}


def positions():
    """{symbol: qty} held at the broker (crypto). None when offline."""
    if not has_key():
        return None
    p = _get_client().get_portfolio()
    return {pos.instrument.symbol: float(pos.quantity)
            for pos in (p.positions or []) if float(pos.quantity) != 0}


def open_orders():
    if not has_key():
        return []
    p = _get_client().get_portfolio()
    out = []
    for o in (p.orders or []):
        status = getattr(o.status, "value", str(o.status))
        if status not in ("NEW", "PARTIALLY_FILLED", "PENDING_REPLACE", "PENDING_CANCEL"):
            continue
        out.append({
            "order_id": str(o.order_id),
            "symbol": o.instrument.symbol if o.instrument else None,
            "side": getattr(o.side, "value", str(o.side)),
            "type": getattr(o.type, "value", str(o.type)),
            "status": status,
            "stop_price": float(o.stop_price) if o.stop_price is not None else None,
            "quantity": float(o.quantity) if o.quantity is not None else None,
        })
    return out


def order_status(order_id):
    if config.DRY_RUN or not has_key():
        return {"status": "FILLED", "filled_quantity": None, "average_price": None}
    o = _get_client().get_order(order_id)
    return {"status": getattr(o.status, "value", str(o.status)),
            "filled_quantity": float(o.filled_quantity) if o.filled_quantity else 0.0,
            "average_price": float(o.average_price) if o.average_price else None}


def cancel_order(order_id):
    print(f"    {_tag()} CANCEL order {order_id}", flush=True)
    if config.DRY_RUN:
        return True
    _get_client().cancel_order(order_id)
    return True


# ---------------- order placement ----------------
def _tag():
    return "[DRY-RUN]" if config.DRY_RUN else "[LIVE]"


def _submit(order_kwargs, describe):
    print(f"    {_tag()} {describe}", flush=True)
    if config.DRY_RUN:
        return {"dry_run": True, "order_id": f"dry-{_uuid.uuid4()}", **order_kwargs}
    from public_api_sdk import (OrderRequest, OrderInstrument, InstrumentType,
                                OrderSide, OrderType)
    from public_api_sdk.models.order import OrderExpirationRequest, TimeInForce
    is_stop = bool(order_kwargs.get("stop_price"))
    expiration = None
    if is_stop:
        expiration = OrderExpirationRequest(
            time_in_force=TimeInForce.GTD,
            expiration_time=_dt.datetime.now(_dt.timezone.utc)
            + _dt.timedelta(days=config.STOP_GTD_DAYS),
        )
    req = OrderRequest(
        order_id=str(_uuid.uuid4()),
        instrument=OrderInstrument(symbol=order_kwargs["symbol"], type=InstrumentType.CRYPTO),
        order_side=OrderSide.BUY if order_kwargs["side"] == "BUY" else OrderSide.SELL,
        order_type=OrderType.STOP if is_stop else OrderType.MARKET,
        quantity=str(order_kwargs["quantity"]),
        **({"stop_price": str(order_kwargs["stop_price"])} if is_stop else {}),
        **({"expiration": expiration} if expiration else {}),
    )
    res = _get_client().place_order(req)
    oid = str(getattr(res, "order_id", req.order_id))
    print(f"    [LIVE] submitted order_id={oid}", flush=True)
    return {"order_id": oid}


def market_buy(symbol, quantity, ref_price):
    return _submit({"symbol": symbol, "side": "BUY", "quantity": quantity},
                   f"BUY {quantity} {symbol} @~{ref_price:.2f} (market)")


def market_sell(symbol, quantity, ref_price, reason=""):
    return _submit({"symbol": symbol, "side": "SELL", "quantity": quantity},
                   f"SELL {quantity} {symbol} @~{ref_price:.2f} (exit: {reason})")


def place_protective_stop(symbol, quantity, stop_price):
    return _submit({"symbol": symbol, "side": "SELL", "quantity": quantity,
                    "stop_price": round(stop_price, 2)},
                   f"STOP-SELL {quantity} {symbol} @ {stop_price:.2f} "
                   f"(protective, GTD {config.STOP_GTD_DAYS}d)")
