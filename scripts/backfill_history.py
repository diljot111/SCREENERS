"""
backfill_history.py
===================
One-time script to populate the last N days of daily candles for every symbol
and pre-compute each symbol's previous-day indicator values into indicator_cache.

Usage:  python scripts/backfill_history.py [days]
        (days defaults to the config history_lookback_days, e.g. 30)
"""

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "python-engine"))

from db import Database, load_config, load_symbols, setup_logging  # noqa: E402
from data_fetcher import AngelOneClient  # noqa: E402
from indicator_engine import compute_yesterday_indicators  # noqa: E402

log = setup_logging()


def main():
    config = load_config()
    symbols = load_symbols()
    db = Database()

    days = int(sys.argv[1]) if len(sys.argv) > 1 else config["screener"]["history_lookback_days"]
    sc = config["screener"]

    client = AngelOneClient(config)
    if not client.authenticate():
        log.error("Authentication failed — cannot backfill via Angel One. "
                  "yfinance fallback will still be attempted per-symbol.")

    total = len(symbols)
    ok, failed = 0, 0
    log.info("Backfilling %d days for %d symbols", days, total)

    for i, sym in enumerate(symbols, 1):
        symbol, token = sym["symbol"], sym["token"]
        try:
            candles = client.fetch_historical(symbol, token, days)
            if candles:
                db.upsert_candles(symbol, candles)
                vals = compute_yesterday_indicators(
                    candles, sc["bollinger_period"], sc["bollinger_std"], sc["ema_period"]
                )
                if vals:
                    db.set_indicator_cache(symbol, vals.get("date"), vals)
                ok += 1
            else:
                failed += 1
                log.warning("No data for %s", symbol)
        except Exception:  # noqa: BLE001
            failed += 1
            log.exception("Backfill failed for %s", symbol)

        if i % 50 == 0:
            log.info("Progress: %d/%d (ok=%d failed=%d)", i, total, ok, failed)
        time.sleep(0.25)  # respect Angel One historical rate limits

    log.info("Backfill complete: ok=%d failed=%d", ok, failed)
    db.close()


if __name__ == "__main__":
    main()
