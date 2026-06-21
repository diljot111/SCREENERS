"""
backfill_yf.py
==============
Fast historical backfill using yfinance (no broker auth needed) so the dashboard
shows REAL daily (end-of-day) candles. Uses yfinance's batch download.

NOTE: this is end-of-day data, not live intraday ticks. True realtime requires
the Angel One WebSocket during market hours (run python-engine/main.py).

Usage:
    python scripts/backfill_yf.py            # curated liquid set (~Nifty large caps)
    python scripts/backfill_yf.py 300        # first 300 symbols from nse_symbols.json
    python scripts/backfill_yf.py all        # every NSE symbol (slow: ~2377)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python-engine"))

import warnings  # noqa: E402

warnings.filterwarnings("ignore")

from db import Database, load_config, load_symbols, setup_logging  # noqa: E402
from indicator_engine import compute_yesterday_indicators  # noqa: E402

log = setup_logging()

# Curated liquid NSE large/mid caps (all present in the Angel One master).
CURATED = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "HINDUNILVR", "SBIN",
    "BHARTIARTL", "ITC", "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT",
    "MARUTI", "HCLTECH", "SUNPHARMA", "TITAN", "ULTRACEMCO", "WIPRO", "NESTLEIND",
    "ONGC", "NTPC", "POWERGRID", "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "ADANIENT",
    "ADANIPORTS", "COALINDIA", "BAJAJFINSV", "GRASIM", "HDFCLIFE", "SBILIFE",
    "BRITANNIA", "DRREDDY", "CIPLA", "EICHERMOT", "HEROMOTOCO", "INDUSINDBK",
    "TECHM", "APOLLOHOSP", "HINDALCO", "BPCL", "TATACONSUM", "SHREECEM",
    "PIDILITIND", "DABUR", "GODREJCP", "HAVELLS", "DLF", "AMBUJACEM", "GAIL",
    "BANKBARODA", "PNB", "IOC", "VEDL", "SIEMENS", "BEL", "TRENT",
]

CHUNK = 40  # tickers per yfinance batch request


def chunked(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def df_to_candles(df, days):
    candles = []
    for idx, row in df.iterrows():
        c = row.get("Close")
        if c is None or c != c:  # NaN guard
            continue
        candles.append({
            "date": idx.strftime("%Y-%m-%d"),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
        })
    return candles[-days:]


def main():
    import yfinance as yf

    cfg = load_config()
    sc = cfg["screener"]
    days = sc["history_lookback_days"]
    db = Database()
    master = load_symbols()
    known = {s["symbol"] for s in master}

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg is None:
        targets = [s for s in CURATED if s in known]
    elif arg.lower() == "all":
        targets = [s["symbol"] for s in master]
    else:
        targets = [s["symbol"] for s in master[: int(arg)]]

    log.info("Backfilling %d symbols via yfinance (real EOD data)…", len(targets))
    ok, failed = 0, 0

    for batch in chunked(targets, CHUNK):
        tickers = [f"{s}.NS" for s in batch]
        try:
            data = yf.download(
                tickers, period="4mo", interval="1d", group_by="ticker",
                auto_adjust=True, threads=True, progress=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Batch download failed (%s) — skipping", exc)
            failed += len(batch)
            continue

        for sym in batch:
            ticker = f"{sym}.NS"
            try:
                sub = data[ticker] if len(tickers) > 1 else data
                sub = sub.dropna(how="all")
                candles = df_to_candles(sub, days)
                if len(candles) >= sc["bollinger_period"]:
                    db.upsert_candles(sym, candles)
                    vals = compute_yesterday_indicators(
                        candles, sc["bollinger_period"], sc["bollinger_std"], sc["ema_period"]
                    )
                    if vals:
                        db.set_indicator_cache(sym, vals.get("date"), vals)
                    ok += 1
                else:
                    failed += 1
            except Exception:  # noqa: BLE001
                failed += 1
        log.info("Progress: ok=%d failed=%d", ok, failed)

    db.close()
    print(f"\nBackfill complete: {ok} symbols loaded with REAL daily data, {failed} skipped.")
    print("Now export the static snapshot:  python scripts/export_static.py")


if __name__ == "__main__":
    main()
