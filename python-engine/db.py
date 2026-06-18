"""
db.py
=====
SQLite persistence layer for the stock screener plus shared helpers
(project paths, config loader, logging setup, IST timezone).

All write operations are serialised behind a single re-entrant lock because
WebSocket callbacks and the APScheduler screener loop run on different threads.
The connection is opened with check_same_thread=False so it can be shared.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytz

# --------------------------------------------------------------------------- #
# Paths & config
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
IST = pytz.timezone("Asia/Kolkata")

_config_cache = None


def load_config():
    """Load config/config.json (config.local.json overrides it if present)."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    base_path = CONFIG_DIR / "config.json"
    with open(base_path, "r", encoding="utf-8") as fh:
        config = json.load(fh)

    local_path = CONFIG_DIR / "config.local.json"
    if local_path.exists():
        with open(local_path, "r", encoding="utf-8") as fh:
            _deep_update(config, json.load(fh))

    _config_cache = config
    return config


def _deep_update(target, overrides):
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
    return target


def load_symbols():
    """Load the NSE symbol master list."""
    path = CONFIG_DIR / "nse_symbols.json"
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def now_ist():
    return datetime.now(IST)


def today_str():
    return now_ist().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

_logging_configured = False


def setup_logging():
    """Configure root logging with console + rotating file handlers (once)."""
    global _logging_configured
    if _logging_configured:
        return logging.getLogger("screener")

    config = load_config()
    log_path = PROJECT_ROOT / config.get("logging", {}).get("path", "logs/screener.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, config.get("logging", {}).get("level", "INFO").upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers if called again.
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    _logging_configured = True
    return logging.getLogger("screener")


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    UNIQUE(symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_candles_symbol_date ON daily_candles(symbol, date);

CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    price REAL,
    ema9 REAL,
    bb_middle REAL,
    bb_upper REAL,
    vwap REAL,
    message_sent BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, date)
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    messages_sent INTEGER DEFAULT 0,
    stocks_matched INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS indicator_cache (
    symbol TEXT PRIMARY KEY,
    date TEXT,
    ema9 REAL,
    bb_middle REAL,
    bb_upper REAL,
    bb_lower REAL,
    vwap_proxy REAL
);
"""


class Database:
    """Thread-safe SQLite wrapper for the screener."""

    def __init__(self, path=None):
        config = load_config()
        rel = path or config["database"]["path"]
        self.path = PROJECT_ROOT / rel
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self.log = logging.getLogger("screener.db")
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ----- daily_candles ----------------------------------------------------- #

    def upsert_candles(self, symbol, candles):
        """
        candles: list of dicts {date, open, high, low, close, volume}
        """
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO daily_candles (symbol, date, open, high, low, close, volume)
                VALUES (:symbol, :date, :open, :high, :low, :close, :volume)
                ON CONFLICT(symbol, date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume
                """,
                [
                    {
                        "symbol": symbol,
                        "date": c["date"],
                        "open": c["open"],
                        "high": c["high"],
                        "low": c["low"],
                        "close": c["close"],
                        "volume": c["volume"],
                    }
                    for c in candles
                ],
            )
            self._conn.commit()

    def get_recent_candles(self, symbol, limit=25):
        """Return the most recent `limit` CLOSED candles ascending by date."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT date, open, high, low, close, volume
                FROM daily_candles
                WHERE symbol = ?
                ORDER BY date DESC
                LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def list_candle_symbols(self):
        """Distinct symbols that have at least one candle, ordered alphabetically."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT symbol FROM daily_candles ORDER BY symbol ASC"
            ).fetchall()
        return [r["symbol"] for r in rows]

    def get_all_candles(self, symbol):
        """Return every candle for a symbol, ascending by date."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT date, open, high, low, close, volume
                FROM daily_candles
                WHERE symbol = ?
                ORDER BY date ASC
                """,
                (symbol,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_alerts(self, limit=50):
        """Most recent alert_log rows across all dates (newest first)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM alert_log
                ORDER BY date DESC, time DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_candles(self, symbol):
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM daily_candles WHERE symbol = ?", (symbol,)
            ).fetchone()
        return row["n"]

    # ----- indicator_cache --------------------------------------------------- #

    def set_indicator_cache(self, symbol, date, values):
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO indicator_cache
                    (symbol, date, ema9, bb_middle, bb_upper, bb_lower, vwap_proxy)
                VALUES (:symbol, :date, :ema9, :bb_middle, :bb_upper, :bb_lower, :vwap_proxy)
                ON CONFLICT(symbol) DO UPDATE SET
                    date=excluded.date, ema9=excluded.ema9, bb_middle=excluded.bb_middle,
                    bb_upper=excluded.bb_upper, bb_lower=excluded.bb_lower,
                    vwap_proxy=excluded.vwap_proxy
                """,
                {
                    "symbol": symbol,
                    "date": date,
                    "ema9": values.get("ema9"),
                    "bb_middle": values.get("bb_middle"),
                    "bb_upper": values.get("bb_upper"),
                    "bb_lower": values.get("bb_lower"),
                    "vwap_proxy": values.get("vwap_proxy"),
                },
            )
            self._conn.commit()

    def get_indicator_cache(self, symbol):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM indicator_cache WHERE symbol = ?", (symbol,)
            ).fetchone()
        return dict(row) if row else None

    def get_all_indicator_cache(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM indicator_cache").fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    # ----- alert_log / dedup ------------------------------------------------- #

    def has_alerted_today(self, symbol, date):
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM alert_log WHERE symbol = ? AND date = ?", (symbol, date)
            ).fetchone()
        return row is not None

    def record_alert(self, symbol, date, time_str, price, ema9, bb_middle, bb_upper, vwap, sent):
        """Insert an alert row. Returns True if newly inserted, False if duplicate."""
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO alert_log
                        (symbol, date, time, price, ema9, bb_middle, bb_upper, vwap, message_sent)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (symbol, date, time_str, price, ema9, bb_middle, bb_upper, vwap, 1 if sent else 0),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_alert_sent(self, symbol, date):
        with self._lock:
            self._conn.execute(
                "UPDATE alert_log SET message_sent = 1 WHERE symbol = ? AND date = ?",
                (symbol, date),
            )
            self._conn.commit()

    def get_today_alerts(self, date):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alert_log WHERE date = ? ORDER BY time ASC", (date,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ----- daily_stats ------------------------------------------------------- #

    def get_messages_sent(self, date):
        with self._lock:
            row = self._conn.execute(
                "SELECT messages_sent FROM daily_stats WHERE date = ?", (date,)
            ).fetchone()
        return row["messages_sent"] if row else 0

    def increment_messages_sent(self, date, by=1):
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO daily_stats (date, messages_sent, stocks_matched)
                VALUES (?, ?, 0)
                ON CONFLICT(date) DO UPDATE SET messages_sent = messages_sent + ?
                """,
                (date, by, by),
            )
            self._conn.commit()

    def increment_stocks_matched(self, date, by=1):
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO daily_stats (date, messages_sent, stocks_matched)
                VALUES (?, 0, ?)
                ON CONFLICT(date) DO UPDATE SET stocks_matched = stocks_matched + ?
                """,
                (date, by, by),
            )
            self._conn.commit()

    def get_daily_stats(self, date):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM daily_stats WHERE date = ?", (date,)
            ).fetchone()
        return dict(row) if row else {"date": date, "messages_sent": 0, "stocks_matched": 0}

    def close(self):
        with self._lock:
            self._conn.close()


if __name__ == "__main__":
    setup_logging()
    db = Database()
    print(f"Database initialised at {db.path}")
    db.close()
