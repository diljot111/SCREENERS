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


def main():
    data = DashboardData()
    out_dir = PROJECT_ROOT / "web" / "data"
    out_dir.mkdir(parents=True, exist_ok=True)

    payloads = {
        "dashboard.json": data.dashboard(limit=10000, flt="all"),
        "alerts.json": {"alerts": data.alerts(200)},
        "stats.json": data.stats(),
    }

    for name, obj in payloads.items():
        path = out_dir / name
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, default=str)
        log.info("Wrote %s (%d bytes)", path, path.stat().st_size)

    db = payloads["dashboard.json"]
    print(f"\nExported static snapshot: {db['total_symbols']} symbols, "
          f"{db['ready_count']} ready-to-buy -> web/data/")


if __name__ == "__main__":
    main()
