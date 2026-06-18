"""
main.py
=======
Orchestrator / entry point for the stock screener.

Daily schedule (Asia/Kolkata):
  09:00  Backfill / update historical candles + pre-compute yesterday indicators
  09:14  Connect WebSocket and subscribe to all symbols
  09:15  Reset forming candles (market open)
  09:20  Start the screener loop (every scan_interval_minutes)
  15:30  Stop the screener loop (market close)
  15:35  Persist today's final forming candle as a closed candle
  15:40  Disconnect WebSocket, send the daily summary

Run with:  python main.py            (scheduler / production mode)
           python main.py --once     (run a single scan now, for testing)
           python main.py --prep      (run the 09:00 prep step now)
"""

import signal
import sys
import time
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from db import Database, IST, load_config, load_symbols, now_ist, setup_logging
from candle_builder import CandleBuilder
from data_fetcher import AngelOneClient, LiveFeed
from indicator_engine import compute_yesterday_indicators
from screener import Screener
from alert_manager import AlertManager

log = setup_logging()


class Orchestrator:
    def __init__(self):
        self.config = load_config()
        self.symbols = load_symbols()
        self.db = Database()
        self.candles = CandleBuilder()
        self.client = AngelOneClient(self.config)
        self.alerts = AlertManager(self.db, self.config)
        self.screener = Screener(self.db, self.candles, self.symbols, self.config)
        self.feed = None
        self.scheduler = BackgroundScheduler(timezone=IST)
        self._screener_job = None
        self._start_time = now_ist()
        self._authenticated = False
        self._shutdown = False

    # ----------------------------------------------------------------------- #
    # Lifecycle steps
    # ----------------------------------------------------------------------- #

    def ensure_auth(self):
        if self._authenticated:
            return True
        ok = self.client.authenticate()
        if not ok:
            msg = "⚠️ System Error: Broker API authentication failed"
            log.error(msg)
            try:
                self.alerts.send_text(msg)
            except Exception:  # noqa: BLE001
                pass
            return False
        self._authenticated = True
        return True

    def prep_historical_and_cache(self):
        """09:00 — refresh historical candles and pre-compute yesterday indicators."""
        log.info("=== Daily prep: historical + indicator cache ===")
        if not self.ensure_auth():
            return

        cfg = self.config["screener"]
        days = cfg.get("history_lookback_days", 30)
        batch = cfg.get("db_batch_size", 500)
        total = len(self.symbols)
        processed = 0

        for i in range(0, total, batch):
            chunk = self.symbols[i:i + batch]
            for sym in chunk:
                symbol, token = sym["symbol"], sym["token"]
                candles = self.client.fetch_historical(symbol, token, days)
                if candles:
                    self.db.upsert_candles(symbol, candles)
                    vals = compute_yesterday_indicators(
                        candles, cfg["bollinger_period"], cfg["bollinger_std"], cfg["ema_period"]
                    )
                    if vals:
                        self.db.set_indicator_cache(symbol, vals.get("date"), vals)
                processed += 1
                time.sleep(0.25)  # respect Angel One historical rate limits
            log.info("Prep progress: %d/%d symbols", processed, total)

        self.screener.load_yesterday_cache()
        log.info("=== Daily prep complete ===")

    def connect_feed(self):
        """09:14 — connect WebSocket and subscribe."""
        log.info("=== Connecting live feed ===")
        if not self.ensure_auth():
            return
        self.feed = LiveFeed(self.client, self.symbols, self._on_tick, self.config)
        self.feed.start()

    def _on_tick(self, symbol, price, cum_volume, o, h, low):
        self.candles.update(symbol, price, cum_volume,
                            exchange_open=o, exchange_high=h, exchange_low=low)

    def market_open(self):
        """09:15 — reset forming candles for the new session."""
        log.info("=== Market open — resetting forming candles ===")
        self.candles.reset()
        self.screener.load_yesterday_cache()

    def start_screener_loop(self):
        """09:20 — start the periodic scan."""
        if self._screener_job is not None:
            return
        interval = self.config["screener"]["scan_interval_minutes"]
        log.info("=== Starting screener loop (every %d min) ===", interval)
        self._screener_job = self.scheduler.add_job(
            self.run_scan,
            IntervalTrigger(minutes=interval, timezone=IST),
            id="screener_loop",
            max_instances=1,
            coalesce=True,
        )

    def run_scan(self):
        """One protected scan cycle."""
        try:
            log.info("Scan cycle starting (live candles: %d)", self.candles.count())
            matches = self.screener.scan()
            if matches:
                sent = self.alerts.process_matches(matches)
                log.info("Scan produced %d matches, %d alerts sent", len(matches), sent)
        except Exception:  # noqa: BLE001
            log.exception("Scan cycle failed — will retry next interval")

    def stop_screener_loop(self):
        """15:30 — stop the scan loop at market close."""
        if self._screener_job is not None:
            self.scheduler.remove_job("screener_loop")
            self._screener_job = None
            log.info("=== Screener loop stopped (market close) ===")

    def persist_final_candles(self):
        """15:35 — store today's forming candles as closed candles."""
        log.info("=== Persisting final candles ===")
        date = now_ist().strftime("%Y-%m-%d")
        snapshot = self.candles.snapshot()
        for symbol, c in snapshot.items():
            if c.get("close"):
                self.db.upsert_candles(symbol, [{
                    "date": date,
                    "open": c.get("open"), "high": c.get("high"),
                    "low": c.get("low"), "close": c.get("close"),
                    "volume": c.get("volume", 0),
                }])
        log.info("Persisted %d final candles", len(snapshot))

    def shutdown_feed_and_summary(self):
        """15:40 — disconnect WebSocket and send the daily summary."""
        log.info("=== Disconnecting feed + daily summary ===")
        if self.feed:
            self.feed.stop()
        uptime = self._uptime_str()
        try:
            self.alerts.send_text(self.alerts.build_daily_summary(uptime))
        except Exception:  # noqa: BLE001
            log.exception("Failed to send daily summary")

    def _uptime_str(self):
        delta = now_ist() - self._start_time
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        minutes = rem // 60
        return f"{hours}h {minutes}m"

    # ----------------------------------------------------------------------- #
    # Scheduling
    # ----------------------------------------------------------------------- #

    def schedule_all(self):
        sc = self.config["screener"]
        open_h, open_m = map(int, sc["market_open"].split(":"))
        close_h, close_m = map(int, sc["market_close"].split(":"))

        jobs = [
            ("prep", self.prep_historical_and_cache, 9, 0),
            ("connect_feed", self.connect_feed, 9, 14),
            ("market_open", self.market_open, open_h, open_m),
            ("start_loop", self.start_screener_loop, 9, 20),
            ("stop_loop", self.stop_screener_loop, close_h, close_m),
            ("persist", self.persist_final_candles, 15, 35),
            ("summary", self.shutdown_feed_and_summary, 15, 40),
        ]
        # Run Monday–Friday only (NSE trading days; holiday calendar handled below).
        for job_id, func, hour, minute in jobs:
            self.scheduler.add_job(
                self._guard(func, job_id),
                CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=IST),
                id=job_id,
                max_instances=1,
                coalesce=True,
            )
        log.info("Scheduled %d daily jobs (Mon–Fri, IST)", len(jobs))

    def _guard(self, func, job_id):
        def wrapped():
            if is_nse_holiday(now_ist().date()):
                log.info("NSE holiday — skipping job '%s'", job_id)
                return
            try:
                func()
            except Exception:  # noqa: BLE001
                log.exception("Scheduled job '%s' failed", job_id)
        return wrapped

    def run(self):
        log.info("Stock screener starting up (%d symbols)", len(self.symbols))
        self.schedule_all()
        self.scheduler.start()

        # If we start up mid-session, catch up so we don't wait until tomorrow.
        self._catch_up_if_mid_session()

        log.info("Scheduler running. Press Ctrl+C to exit.")
        try:
            while not self._shutdown:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self.graceful_shutdown()

    def _catch_up_if_mid_session(self):
        now = now_ist()
        if is_nse_holiday(now.date()) or now.weekday() >= 5:
            return
        sc = self.config["screener"]
        open_t = now.replace(hour=int(sc["market_open"].split(":")[0]),
                             minute=int(sc["market_open"].split(":")[1]),
                             second=0, microsecond=0)
        close_t = now.replace(hour=int(sc["market_close"].split(":")[0]),
                              minute=int(sc["market_close"].split(":")[1]),
                              second=0, microsecond=0)
        if open_t <= now <= close_t:
            log.info("Started mid-session — catching up (prep, connect, scan loop)")
            self.prep_historical_and_cache()
            self.connect_feed()
            self.start_screener_loop()

    def graceful_shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        log.info("Shutting down gracefully…")
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        if self.feed:
            self.feed.stop()
        self.db.close()
        log.info("Shutdown complete.")


# --------------------------------------------------------------------------- #
# NSE holiday calendar
# --------------------------------------------------------------------------- #

# Static list of NSE trading holidays. Update yearly from the NSE circular.
NSE_HOLIDAYS_2026 = {
    "2026-01-26",  # Republic Day
    "2026-02-16",  # Maha Shivaratri (example)
    "2026-03-06",  # Holi
    "2026-03-21",  # Id-Ul-Fitr (example)
    "2026-04-01",  # Annual closing of accounts (example)
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day
    "2026-10-02",  # Gandhi Jayanti
    "2026-11-09",  # Diwali (example)
    "2026-12-25",  # Christmas
}


def is_nse_holiday(date_obj):
    if date_obj.weekday() >= 5:  # Saturday/Sunday
        return True
    return date_obj.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026


# --------------------------------------------------------------------------- #
# Entry point with crash auto-restart
# --------------------------------------------------------------------------- #

def _install_signal_handlers(orch):
    def handler(signum, frame):  # noqa: ARG001
        log.info("Received signal %s", signum)
        orch.graceful_shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def main():
    args = sys.argv[1:]

    if "--once" in args:
        orch = Orchestrator()
        _install_signal_handlers(orch)
        if not orch.ensure_auth():
            return
        orch.prep_historical_and_cache()
        orch.connect_feed()
        log.info("Waiting 20s for initial ticks before single scan…")
        time.sleep(20)
        orch.run_scan()
        orch.graceful_shutdown()
        return

    if "--prep" in args:
        orch = Orchestrator()
        _install_signal_handlers(orch)
        orch.prep_historical_and_cache()
        orch.graceful_shutdown()
        return

    # Production mode with auto-restart on crash.
    while True:
        orch = Orchestrator()
        _install_signal_handlers(orch)
        try:
            orch.run()
            break  # clean exit
        except Exception:  # noqa: BLE001
            log.exception("Orchestrator crashed — restarting in 30s")
            try:
                orch.graceful_shutdown()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(30)


if __name__ == "__main__":
    main()
