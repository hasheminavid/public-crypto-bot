"""Daily Claude review — runs INSIDE the bot process once per UTC day (a
separate Railway cron can't share the volume, so the loop hosts it).

Flow: run review.py -> send report + current params + hard bounds to the
Anthropic API -> parse a strict-JSON decision -> clamp to bounds -> write
STATE_DIR/params.json (picked up live and on restart) -> Telegram summary.

DISCIPLINE (enforced in CODE, not trust):
  * tuning gate: below config.MIN_TRADES_FOR_TUNING closed trades, any
    "adjust" from the model is forced to HOLD
  * only whitelisted params, hard-clamped to config.HARD_BOUNDS
  * a failed review never touches params and never crashes the bot
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

import config
import state as st
from alerts import alert, log

PARAMS_FILE = os.path.join(config.STATE_DIR, "params.json")
REVIEW_HOUR_UTC = int(os.environ.get("REVIEW_HOUR_UTC", "21"))  # after US close
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

PROMPT = """You are the daily reviewer for a small automated RSI(2) mean-reversion
CRYPTO bot (BTC/ETH, daily UTC bars) trading a small STUDY account on Public.com.
Crypto backtests in this project's history showed trend strategies were overfit
traps; treat every result with suspicion and favour holding parameters. Your job is to protect discipline,
not to chase performance. Respond ONLY with JSON, no other text:
{"action":"hold"|"adjust","changes":{"PARAM":value,...},"reason":"<max 300 chars>"}

Rules you must follow:
- The DEFAULT is hold. Most days the correct answer is hold.
- TUNING GATE: %(gate)s. If LOCKED you MUST respond {"action":"hold"...}.
- You may only change these params, within these hard bounds: %(bounds)s
- Never loosen entry filters to manufacture trades. Low activity is by design.
- One small change at a time; cite the mechanical evidence for it.
"""


def _closed_trades():
    return [t for t in st.load().get("trades", []) if t.get("side") == "SELL"]


def _report_text():
    r = subprocess.run([sys.executable, "review.py"], capture_output=True,
                       text=True, timeout=300, cwd=os.path.dirname(os.path.abspath(__file__)))
    return (r.stdout or r.stderr)[-6000:]


def _current_params():
    return {k: getattr(config, k) for k in config.HARD_BOUNDS}


def _ask_claude(report, gate_open):
    gate = (f"OPEN ({len(_closed_trades())} closed trades)" if gate_open
            else f"LOCKED ({len(_closed_trades())}/{config.MIN_TRADES_FOR_TUNING} closed trades)")
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 500,
        "system": PROMPT % {"gate": gate, "bounds": json.dumps(config.HARD_BOUNDS)},
        "messages": [{"role": "user", "content":
                      f"Daily report:\n{report}\n\nCurrent params: "
                      f"{json.dumps(_current_params())}\n\nYour JSON decision:"}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as f:
        out = json.loads(f.read())
    txt = out["content"][0]["text"].strip()
    if "```" in txt:
        txt = txt.split("```")[1]
        if txt.startswith("json"):
            txt = txt[4:]
    return json.loads(txt.strip())


def _apply(changes):
    """Clamp to HARD_BOUNDS, persist, and update the live process."""
    applied = {}
    for k, v in (changes or {}).items():
        if k not in config.HARD_BOUNDS:
            continue
        lo, hi = config.HARD_BOUNDS[k]
        try:
            v = type(getattr(config, k))(v)
        except (TypeError, ValueError):
            continue
        v = min(max(v, lo), hi)
        applied[k] = v
    if not applied:
        return {}
    try:
        with open(PARAMS_FILE) as f:
            cur = json.load(f)
    except Exception:
        cur = {}
    cur.setdefault("params", {}).update(applied)
    cur["updated"] = datetime.now(timezone.utc).isoformat()
    tmp = PARAMS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f, indent=2)
    os.replace(tmp, PARAMS_FILE)
    for k, v in applied.items():
        setattr(config, k, v)          # live process picks it up immediately
    return applied


def maybe_run(force=False):
    """Called from the bot loop. Runs at most once per UTC day, at/after
    REVIEW_HOUR_UTC. Never raises. A missing API key does NOT consume the
    day's slot (so adding the key later still gets today's review).
    force=True (CLI --force) runs immediately without consuming the slot."""
    try:
        if not force:
            now = datetime.now(timezone.utc)
            if now.hour < REVIEW_HOUR_UTC:
                return
            s = st.load()
            today = st.today_utc()
            if s.get("last_review_day") == today:
                return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log("daily review skipped: ANTHROPIC_API_KEY not set")
            return
        if not force:
            s["last_review_day"] = today
            st.save(s)
        report = _report_text()
        gate_open = len(_closed_trades()) >= config.MIN_TRADES_FOR_TUNING
        d = _ask_claude(report, gate_open)
        action = d.get("action", "hold")
        reason = str(d.get("reason", ""))[:300]
        if action == "adjust" and not gate_open:
            alert(f"DAILY REVIEW: model proposed changes but tuning gate is LOCKED "
                  f"-> forced HOLD. Reason given: {reason}")
            return
        if action == "adjust":
            applied = _apply(d.get("changes"))
            if applied:
                alert(f"DAILY REVIEW: adjusted {applied} | {reason}")
            else:
                alert(f"DAILY REVIEW: proposed changes were out of bounds -> HOLD | {reason}")
        else:
            alert(f"DAILY REVIEW: HOLD | {reason}")
    except Exception as e:
        log(f"daily review failed (non-fatal): {e}")


if __name__ == "__main__":
    maybe_run(force="--force" in sys.argv)
