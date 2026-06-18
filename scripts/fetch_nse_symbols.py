"""
fetch_nse_symbols.py
====================
One-time (run periodically to refresh) script that downloads Angel One's
instrument master file and filters it to NSE common-equity stocks, then writes
config/nse_symbols.json.

Usage:  python scripts/fetch_nse_symbols.py
"""

import json
import sys
from pathlib import Path

import requests

# Make python-engine importable so we can reuse path constants.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "python-engine"))

from db import CONFIG_DIR  # noqa: E402

INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)

# Suffixes/patterns that indicate non-common-equity instruments to exclude.
EXCLUDE_NAME_TOKENS = (
    "ETF", "BEES", "GILT", "LIQUID", "BOND", "SDL", "GS20", "GOI",
    "-RE", "-PP", "-N", "-W", "-BL",
)


def is_common_equity(row):
    if row.get("exch_seg") != "NSE":
        return False
    # Angel One marks cash equities with instrumenttype empty and symbol ending -EQ.
    tradingsymbol = (row.get("symbol") or "").upper()
    if not tradingsymbol.endswith("-EQ"):
        return False
    instrumenttype = (row.get("instrumenttype") or "").strip()
    if instrumenttype not in ("", "EQ"):
        return False
    name = (row.get("name") or "").upper()
    for tok in EXCLUDE_NAME_TOKENS:
        if tok in tradingsymbol or tok in name:
            # Allow plain -EQ; only exclude clear non-equity markers.
            if tok != "-EQ":
                return False
    return True


def main():
    print(f"Downloading instrument master from:\n  {INSTRUMENT_MASTER_URL}")
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    print(f"Total instruments: {len(data)}")

    symbols = []
    seen = set()
    for row in data:
        if not is_common_equity(row):
            continue
        tradingsymbol = row["symbol"]            # e.g. RELIANCE-EQ
        base = tradingsymbol.replace("-EQ", "")  # e.g. RELIANCE
        if base in seen:
            continue
        seen.add(base)
        symbols.append({
            "symbol": base,
            "token": str(row["token"]),
            "tradingsymbol": tradingsymbol,
            "name": row.get("name", base),
        })

    symbols.sort(key=lambda s: s["symbol"])
    out_path = CONFIG_DIR / "nse_symbols.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(symbols, fh, indent=2)

    print(f"Wrote {len(symbols)} NSE equity symbols to {out_path}")


if __name__ == "__main__":
    main()
