"""
export_static.py
================
Export the dashboard data to static JSON files in web/data/ so the frontend can
be hosted on a static host (Vercel, Netlify, GitHub Pages) with NO Python
backend. The frontend tries the live /api/* first and falls back to these files.

Writes:
    web/data/dashboard.json   (all symbols' candle + indicator series + signal)
    web/data/alerts.json      ({"alerts": [...]})
    web/data/stats.json       (today's daily stats)

Run after seeding/backfilling the DB:
    python scripts/seed_demo_data.py     # demo data (or backfill_history.py for real)
    python scripts/export_static.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python-engine"))

from db import PROJECT_ROOT, setup_logging  # noqa: E402
from web_server import DashboardData  # noqa: E402

log = setup_logging()


def safe_name(symbol):
    """Filesystem/URL-safe filename for a symbol (matches the frontend)."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in symbol)


def main():
    data = DashboardData()
    out_dir = PROJECT_ROOT / "web" / "data"
    candles_dir = out_dir / "candles"
    out_dir.mkdir(parents=True, exist_ok=True)
    candles_dir.mkdir(parents=True, exist_ok=True)

    # Lightweight summary list (all symbols) + small top-level files.
    dash = data.dashboard(limit=100000, flt="all", summary=True)
    payloads = {
        "dashboard.json": dash,
        "alerts.json": {"alerts": data.alerts(200)},
        "stats.json": data.stats(),
    }
    for name, obj in payloads.items():
        path = out_dir / name
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, default=str)
        log.info("Wrote %s (%d bytes)", path, path.stat().st_size)

    # Per-symbol candle/indicator series for lazy chart loading on static hosts.
    written = 0
    for symbol in data.db.list_candle_symbols():
        payload = data.symbol_series(symbol)
        if payload is None:
            continue
        with open(candles_dir / f"{safe_name(symbol)}.json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
        written += 1

    print(f"\nExported static snapshot: {dash['total_symbols']} symbols, "
          f"{dash['ready_count']} ready-to-buy, {written} chart files -> web/data/")


if __name__ == "__main__":
    main()
