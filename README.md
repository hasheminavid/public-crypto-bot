# Public.com CRYPTO mean-reversion bot — small STUDY account

RSI(2) mean reversion on daily UTC bars for BTC/ETH via Public.com's crypto API.
Same safety architecture as `public-mr-bot`, adapted for a 24/7 market.

## ⚠️ Read first
- **This is an experiment, not a proven edge.** Project backtests found crypto
  strategies to be the classic overfit trap. This exists to STUDY the behaviour
  with a small amount you can afford to lose.
- **DRY_RUN=true by default** — logs orders, sends nothing.
- Hard caps: `MAX_POSITION_NOTIONAL` ($100/position default), 0.5% risk per
  trade, max 2 positions, 3% daily circuit breaker.

## The rules (deterministic — no LLM in the trading loop)
- Universe: `BTC, ETH` (liquid majors only)
- Entry: daily close > SMA200 AND RSI(2) < 10 (oversold dip in an uptrend)
- Exit: daily close > SMA5 (mean touch), or protective stop at entry − 3×ATR(14)
- Stops: broker-side STOP (GTD 30d) attempted first; if Public rejects STOP for
  crypto, the bot enforces the stop itself on every 5-minute poll via live quotes
- 24/7: no market-hours windows; one strategy pass per completed UTC daily bar;
  reconciliation, pending-fill confirmation, breaker and stop checks every poll

## Safety model (mirrors public-mr-bot)
Broker reconciliation every cycle (no double-sells), real-fill confirmation
before stops are computed, stop cancelled before mean-touch exits, Telegram
alerts (`TG_TOKEN`/`TG_CHAT`) with ACTION NEEDED instructions, daily Claude
review with a 30-trade tuning gate and hard parameter bounds.

## Deploy (Railway)
Service from this repo + attach a Volume, set `STATE_DIR=/data`. Variables:
`PUBLIC_API_SECRET`, `PUBLIC_ACCOUNT`, `TG_TOKEN`, `TG_CHAT`,
`ANTHROPIC_API_KEY`, `STATE_DIR=/data`, and `DRY_RUN=true` until you have
watched it for a while and deliberately flip it yourself.

## Tests
`python tests/test_logic.py` — offline, mocked broker.
