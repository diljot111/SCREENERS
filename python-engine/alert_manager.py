"""
alert_manager.py
================
Owns alert delivery: deduplication, the 400/day cap, message formatting,
a sequential queue with a 2s gap between sends, and retry-on-failure when the
WhatsApp service is unreachable.
"""

import logging
import threading
import time

import requests

from db import now_ist

log = logging.getLogger("screener.alerts")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class AlertManager:
    def __init__(self, db, config):
        self.db = db
        wa = config["whatsapp"]
        self.service_url = wa["service_url"]
        self.phone = str(wa["target_phone"])
        self.max_daily = wa["max_daily_messages"]
        self.delay = wa["delay_between_messages_sec"]
        self.max_retries = wa.get("max_retries", 5)
        self.retry_interval = wa.get("retry_interval_sec", 30)
        self._lock = threading.Lock()

    # ----------------------------------------------------------------------- #
    # Public API
    # ----------------------------------------------------------------------- #

    def process_matches(self, matches):
        """
        Given a list of match dicts from the screener, send alerts for the ones
        that are new today, respecting the daily cap. Sends sequentially with a
        delay between messages. Returns the number of alerts actually sent.
        """
        if not matches:
            return 0

        date = now_ist().strftime("%Y-%m-%d")
        sent = 0

        with self._lock:
            for match in matches:
                symbol = match["symbol"]

                # Dedup: already alerted today?
                if self.db.has_alerted_today(symbol, date):
                    continue

                # Daily cap check (recheck each iteration).
                if self.db.get_messages_sent(date) >= self.max_daily:
                    log.warning("Daily message cap (%d) reached — skipping remaining alerts",
                                self.max_daily)
                    break

                # Reserve the dedup slot first (atomic insert). If another path
                # already inserted it, skip.
                reserved = self.db.record_alert(
                    symbol, date,
                    now_ist().strftime("%H:%M:%S"),
                    match.get("price"), match.get("ema9"),
                    match.get("bb_middle"), match.get("bb_upper"),
                    match.get("vwap"), sent=False,
                )
                if not reserved:
                    continue

                alert_number = self.db.get_messages_sent(date) + 1
                message = self.format_alert(match, alert_number)

                ok = self._send_with_retry(message)
                if ok:
                    self.db.mark_alert_sent(symbol, date)
                    self.db.increment_messages_sent(date, 1)
                    self.db.increment_stocks_matched(date, 1)
                    sent += 1
                    log.info("Alert sent for %s (%d/%d today)",
                             symbol, alert_number, self.max_daily)
                else:
                    log.error("Failed to deliver alert for %s after retries", symbol)

                # Rate-limit gap between consecutive messages.
                time.sleep(self.delay)

        return sent

    def send_text(self, message):
        """Send an arbitrary text message (used for system errors / daily summary).
        Counts against the daily cap only if under it; system messages still try."""
        return self._send_with_retry(message)

    # ----------------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------------- #

    def _send_with_retry(self, message):
        payload = {"phone": self.phone, "message": message}
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(self.service_url, json=payload, timeout=15)
                if resp.status_code == 200 and resp.json().get("success"):
                    return True
                log.warning("WhatsApp service returned %s: %s",
                            resp.status_code, resp.text[:200])
            except requests.RequestException as exc:
                log.warning("WhatsApp service unreachable (attempt %d/%d): %s",
                            attempt, self.max_retries, exc)
            if attempt < self.max_retries:
                time.sleep(self.retry_interval)
        return False

    def format_alert(self, match, alert_number):
        now = now_ist()
        date_str = f"{now.day:02d}-{MONTHS[now.month - 1]}-{now.year}"
        time_str = now.strftime("%I:%M %p").lstrip("0")

        def fmt(v):
            return f"₹{v:,.2f}" if v is not None else "N/A"

        return (
            "🟢 STOCK ALERT\n\n"
            f"Symbol: {match['symbol']}\n"
            f"Name: {match.get('name', match['symbol'])}\n"
            f"NSE Code: {match['symbol']}\n\n"
            f"💰 Price: {fmt(match.get('price'))}\n"
            f"📊 9 EMA: {fmt(match.get('ema9'))}\n"
            f"📊 BB Middle: {fmt(match.get('bb_middle'))}\n"
            f"📊 BB Upper: {fmt(match.get('bb_upper'))}\n"
            f"📊 VWAP: {fmt(match.get('vwap'))}\n\n"
            "✅ Signal: 9EMA crossed above BB Middle + VWAP crossed above BB Upper\n"
            f"🕐 Time: {time_str} IST\n"
            f"📅 Date: {date_str}\n\n"
            "[Daily Timeframe | Long Term Signal]\n"
            f"Alert {alert_number}/{self.max_daily} today"
        )

    def build_daily_summary(self, uptime_str=""):
        date = now_ist().strftime("%Y-%m-%d")
        now = now_ist()
        date_str = f"{now.day} {MONTHS[now.month - 1]} {now.year}"
        stats = self.db.get_daily_stats(date)
        alerts = self.db.get_today_alerts(date)

        lines = [
            f"📊 DAILY SUMMARY — {date_str}\n",
            f"Total Alerts Sent: {stats['messages_sent']}/{self.max_daily}",
            f"Stocks Matched: {stats['stocks_matched']}\n",
            "Top Matches:",
        ]
        if alerts:
            for i, a in enumerate(alerts, 1):
                price = a.get("price")
                price_str = f"₹{price:,.2f}" if price is not None else "N/A"
                lines.append(f"{i}. {a['symbol']} — {price_str}")
        else:
            lines.append("(no matches today)")

        lines.append("")
        lines.append(f"System Status: ✅ Running | Uptime: {uptime_str}")
        return "\n".join(lines)
