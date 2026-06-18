"""
seed_demo_data.py
=================
Populate the database with SYNTHETIC daily candles so the web dashboard has
something to display without a live broker feed / market hours.

It generates ~40 weekday candles for each symbol in config/nse_symbols.json,
crafts a couple of them into a fresh "ready to buy" crossover (flat base + a
breakout candle), writes the previous-day indicator cache, and inserts a sample
alert row so the Notifications panel is populated too.

This is for DEMO / UI testing ONLY — it is not market data. Run the real
backfill_history.py for actual candles.

    python scripts/seed_demo_data.py
"""

import math
import os
import sys
from datetime import timedelta

# allow importing the engine modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python-engine"))

from db import Database, load_config, load_symbols, now_ist, setup_logging  # noqa: E402
from indicator_engine import (  # noqa: E402
    compute_indicator_series,
    compute_yesterday_indicators,
    evaluate_daily_signal,
)

log = setup_logging()

N_CANDLES = 40  # weekdays of history to generate


def weekday_dates(n, end_date):
    """Return the last `n` weekday dates (ascending) ending at end_date."""
    dates = []
    d = end_date
    while len(dates) < n:
        if d.weekday() < 5:  # Mon-Fri
            dates.append(d)
        d -= timedelta(days=1)
    return list(reversed(dates))


def make_candles(base, dates, kind):
    """
    Deterministic synthetic OHLC.

    kind:
      "breakout" -> flat base then a strong final candle (triggers ready-to-buy)
      "sideways" -> oscillating, no fresh crossover
      "uptrend"  -> steady climb (already extended, usually not a fresh cross)
      "downtrend"-> steady decline
    """
    candles = []
    price = base
    for i, d in enumerate(dates):
        last = i == len(dates) - 1
        # gentle deterministic wiggle (sine) so bands have a little width
        wiggle = math.sin(i * 0.7) * base * 0.004

        if kind == "breakout":
            # gentle downward drift so the fast EMA dips just BELOW the BB middle
            # on the candle before the breakout (sets up a fresh upward crossover).
            price = base * (1 - 0.0018 * i) + wiggle
            if last:
                # strong breakout candle: open near the drifted level, close well
                # above the upper band -> EMA crosses up through middle, VWAP
                # (typical price) crosses up through the upper band.
                o = base * (1 - 0.0018 * (i - 1))
                c = base * 1.05
                hi = c * 1.005
                lo = o * 0.998
                candles.append(_row(d, o, hi, lo, c, 1_800_000))
                continue
        elif kind == "sideways":
            price = base + math.sin(i * 0.5) * base * 0.012
        elif kind == "uptrend":
            price = base * (1 + 0.004 * i) + wiggle
        elif kind == "downtrend":
            price = base * (1 - 0.003 * i) + wiggle

        o = price * (1 - 0.003)
        c = price * (1 + 0.003 if i % 2 == 0 else 1 - 0.002)
        hi = max(o, c) * 1.004
        lo = min(o, c) * 0.996
        candles.append(_row(d, o, hi, lo, c, 900_000 + (i % 7) * 25_000))
    return candles


def _row(d, o, h, l, c, vol):
    return {
        "date": d.strftime("%Y-%m-%d"),
        "open": round(o, 2),
        "high": round(h, 2),
        "low": round(l, 2),
        "close": round(c, 2),
        "volume": int(vol),
    }


def main():
    cfg = load_config()
    sc = cfg["screener"]
    db = Database()
    symbols = load_symbols()

    dates = weekday_dates(N_CANDLES, now_ist().date())

    # assign a pattern per symbol; first two are deliberate breakouts.
    patterns = ["breakout", "breakout", "sideways", "uptrend", "downtrend"]
    bases = [2850, 3900, 1650, 1480, 1180]

    ready_syms = []
    for idx, sym in enumerate(symbols):
        symbol = sym["symbol"]
        base = bases[idx % len(bases)]
        kind = patterns[idx % len(patterns)]
        candles = make_candles(base, dates, kind)
        db.upsert_candles(symbol, candles)

        # previous-day indicator cache (used by the live screener)
        yvals = compute_yesterday_indicators(
            candles[:-1], sc["bollinger_period"], sc["bollinger_std"], sc["ema_period"]
        )
        if yvals:
            db.set_indicator_cache(symbol, yvals.get("date"), yvals)

        series = compute_indicator_series(
            candles, sc["bollinger_period"], sc["bollinger_std"], sc["ema_period"]
        )
        sig = evaluate_daily_signal(series)
        status = "READY ✅" if (sig and sig["ready"]) else (kind)
        if sig and sig["ready"]:
            ready_syms.append((symbol, sig))
        log.info("Seeded %-10s base=%-6s pattern=%-9s -> %s", symbol, base, kind, status)

    # insert a sample alert (notifications panel) for the first ready symbol
    today = now_ist().strftime("%Y-%m-%d")
    if ready_syms:
        symbol, sig = ready_syms[0]
        db.record_alert(
            symbol, today, now_ist().strftime("%H:%M:%S"),
            sig["price"], sig["ema9"], sig["bb_middle"], sig["bb_upper"], sig["vwap"],
            sent=True,
        )
        db.increment_messages_sent(today, 1)
        db.increment_stocks_matched(today, 1)
        log.info("Inserted sample alert for %s", symbol)

    db.close()
    print(f"\nDone. Seeded {len(symbols)} symbols, {len(ready_syms)} ready-to-buy.")
    print("Start the dashboard:  cd python-engine && python web_server.py")


if __name__ == "__main__":
    main()
