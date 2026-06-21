"""
web_server.py
=============
A dependency-free web dashboard for the stock screener.

Built on Python's stdlib ``http.server`` (no Flask / extra installs needed) so it
runs with the same venv the engine already uses. It serves:

  * the static dashboard in ``web/``           (GET /)
  * a small JSON API the dashboard consumes:

      GET  /api/health
      GET  /api/dashboard?limit=&filter=        summary + signal for every symbol
      GET  /api/candles/<SYMBOL>                full candle + indicator series
      GET  /api/alerts                          recent alerts (notifications sent)
      GET  /api/stats                           today's daily stats
      POST /api/send-test                       send a test WhatsApp notification

Run:  python web_server.py            (from python-engine/, defaults to :8000)
      python web_server.py 8080       (custom port)

The "ready to buy" signal shown here is evaluated from the closed daily candles
via indicator_engine.evaluate_daily_signal — the same crossover logic the live
screener uses, so the dashboard is consistent with the alerts that get sent.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

from db import Database, PROJECT_ROOT, load_config, load_symbols, now_ist, setup_logging
from indicator_engine import compute_indicator_series, evaluate_daily_signal

log = setup_logging()

WEB_DIR = PROJECT_ROOT / "web"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class DashboardData:
    """Reads the DB + config and assembles the payloads the dashboard needs."""

    def __init__(self):
        self.config = load_config()
        self.db = Database()
        self.symbols = load_symbols()
        self.name_by_symbol = {s["symbol"]: s.get("name", s["symbol"]) for s in self.symbols}
        sc = self.config["screener"]
        self.bb_period = sc["bollinger_period"]
        self.bb_std = sc["bollinger_std"]
        self.ema_period = sc["ema_period"]
        # WhatsApp microservice base URL (derive from the send-message URL).
        wa_url = self.config["whatsapp"]["service_url"]
        self.wa_base = wa_url.rsplit("/", 1)[0] if "/send-message" in wa_url else wa_url
        self.wa_send_url = wa_url
        self.target_phone = str(self.config["whatsapp"]["target_phone"])

    # ----- per-symbol series ------------------------------------------------- #

    def symbol_series(self, symbol):
        candles = self.db.get_all_candles(symbol)
        if not candles:
            return None
        series = compute_indicator_series(candles, self.bb_period, self.bb_std, self.ema_period)
        signal = evaluate_daily_signal(series)
        return {
            "symbol": symbol,
            "name": self.name_by_symbol.get(symbol, symbol),
            "series": series,
            "signal": signal,
        }

    # ----- dashboard summary ------------------------------------------------- #

    def dashboard(self, limit=10000, flt="all", summary=False):
        """
        Card data for every symbol that has candles.

        summary=True omits the heavy per-candle `series` (returns only the signal
        + last price + change%), so the dashboard can list thousands of stocks in
        a small payload and lazy-load each chart's data on demand. summary=False
        embeds the full series (used for the static Vercel snapshot).
        """
        symbols = self.db.list_candle_symbols()
        cards = []
        ready_count = 0

        for symbol in symbols:
            candles = self.db.get_all_candles(symbol)
            if not candles:
                continue
            series = compute_indicator_series(candles, self.bb_period, self.bb_std, self.ema_period)
            sig = evaluate_daily_signal(series)
            if sig and sig.get("ready"):
                ready_count += 1
            if not _passes_filter(sig, flt):
                continue

            name = self.name_by_symbol.get(symbol, symbol)
            if summary:
                change_pct = None
                if len(series) >= 2 and series[-1]["close"] and series[-2]["close"]:
                    change_pct = (series[-1]["close"] - series[-2]["close"]) / series[-2]["close"] * 100
                cards.append({"symbol": symbol, "name": name, "signal": sig,
                              "change_pct": change_pct})
            else:
                cards.append({"symbol": symbol, "name": name, "series": series, "signal": sig})
            if len(cards) >= limit:
                break

        return {
            "generated_at": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "total_symbols": len(symbols),
            "ready_count": ready_count,
            "shown": len(cards),
            "filter": flt,
            "indicators": {
                "bb_period": self.bb_period,
                "bb_std": self.bb_std,
                "ema_period": self.ema_period,
            },
            "cards": cards,
        }

    # ----- notifications ----------------------------------------------------- #

    def alerts(self, limit=50):
        rows = self.db.get_recent_alerts(limit)
        for r in rows:
            r["name"] = self.name_by_symbol.get(r["symbol"], r["symbol"])
        return rows

    def stats(self):
        date = now_ist().strftime("%Y-%m-%d")
        s = self.db.get_daily_stats(date)
        s["max_daily_messages"] = self.config["whatsapp"]["max_daily_messages"]
        return s

    # ----- WhatsApp proxy + sending ----------------------------------------- #

    def wa_get(self, path):
        """Proxy a GET to the WhatsApp microservice; tolerate it being down."""
        try:
            resp = requests.get(f"{self.wa_base}{path}", timeout=8)
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - includes connection + non-JSON (404) errors
            return {"error": f"whatsapp endpoint unavailable ({exc})",
                    "service_url": self.wa_base}

    def wa_history(self, limit=100):
        """Sent-message history from the WhatsApp service, falling back to the DB
        alert_log (sent rows) for older service builds that lack /history."""
        h = self.wa_get("/history?limit=" + str(limit))
        if isinstance(h, dict) and "messages" in h:
            return h
        # Fallback: synthesise from alert_log rows that were sent.
        rows = [r for r in self.db.get_recent_alerts(limit) if r.get("message_sent")]
        messages = [{
            "phone": self.target_phone,
            "message": f"🟢 STOCK ALERT — READY TO BUY  {r['symbol']}",
            "ts": f"{r['date']}T{r.get('time', '')}",
            "messageId": None,
            "success": True,
        } for r in rows]
        return {"messages": messages, "source": "db_alert_log"}

    def wa_status(self):
        """Status with a /health fallback (older service builds lack /status)."""
        st = self.wa_get("/status")
        if "connected" not in st:
            h = self.wa_get("/health")
            if "connected" in h:
                return {"connected": h["connected"], "queued": h.get("queued", 0),
                        "me": None, "legacy": True}
            return st
        return st

    def _format_buy_message(self, card, n, total):
        sig = card["signal"] or {}

        def fmt(v):
            return f"₹{v:,.2f}" if v is not None else "N/A"

        return (
            "🟢 STOCK ALERT — READY TO BUY\n\n"
            f"Symbol: {card['symbol']}\n"
            f"Name: {card.get('name', card['symbol'])}\n\n"
            f"💰 Price: {fmt(sig.get('price'))}\n"
            f"📊 9 EMA: {fmt(sig.get('ema9'))}\n"
            f"📊 BB Middle: {fmt(sig.get('bb_middle'))}\n"
            f"📊 BB Upper: {fmt(sig.get('bb_upper'))}\n\n"
            "✅ Signal: 9 EMA above BB Middle (uptrend) + Close above BB Upper (breakout)\n"
            f"📅 As of: {sig.get('date')}\n"
            "[Daily EOD signal]\n"
            f"Alert {n}/{total}"
        )

    def send_ready(self):
        """
        Send a WhatsApp alert for current ready-to-buy stocks to the configured
        number, respecting the daily message cap (max_daily_messages, default 400)
        and de-duplicating: a symbol already alerted today is skipped. Records each
        send in alert_log. Returns a summary + per-symbol result list.
        """
        ready = [c for c in self.dashboard(limit=100000, flt="ready")["cards"]]
        if not ready:
            return {"sent": 0, "ready": 0, "results": [],
                    "note": "No stocks are ready to buy right now."}

        date = now_ist().strftime("%Y-%m-%d")
        max_daily = int(self.config["whatsapp"]["max_daily_messages"])
        already_sent_today = self.db.get_messages_sent(date)
        remaining = max(0, max_daily - already_sent_today)

        results = []
        sent = 0
        skipped_dupe = 0
        capped = 0

        for card in ready:
            symbol = card["symbol"]

            # Dedup: don't re-send a symbol already alerted today.
            if self.db.has_alerted_today(symbol, date):
                skipped_dupe += 1
                results.append({"symbol": symbol, "ok": False, "detail": "already alerted today"})
                continue

            # Respect the daily cap (never exceed max_daily).
            if self.db.get_messages_sent(date) >= max_daily:
                capped += 1
                results.append({"symbol": symbol, "ok": False, "detail": "daily cap reached"})
                continue

            alert_no = self.db.get_messages_sent(date) + 1
            message = self._format_buy_message(card, alert_no, max_daily)
            try:
                resp = requests.post(
                    self.wa_send_url,
                    json={"phone": self.target_phone, "message": message},
                    timeout=20,
                )
                ok = resp.status_code == 200 and resp.json().get("success")
                detail = resp.text[:200]
            except requests.RequestException as exc:
                ok, detail = False, str(exc)

            sig = card["signal"] or {}
            self.db.record_alert(
                symbol, date, now_ist().strftime("%H:%M:%S"),
                sig.get("price"), sig.get("ema9"), sig.get("bb_middle"),
                sig.get("bb_upper"), sig.get("vwap"), sent=bool(ok),
            )
            if ok:
                self.db.increment_messages_sent(date, 1)
                self.db.increment_stocks_matched(date, 1)
                sent += 1
            results.append({"symbol": symbol, "ok": bool(ok), "detail": detail})

        return {
            "sent": sent,
            "ready": len(ready),
            "skipped_already_sent": skipped_dupe,
            "skipped_cap": capped,
            "sent_today": self.db.get_messages_sent(date),
            "max_daily": max_daily,
            "remaining_today": max(0, max_daily - self.db.get_messages_sent(date)),
            "phone": self.target_phone,
            "results": results,
        }

    def send_test(self):
        """Fire a test message at the WhatsApp service so 'notifications' are visibly working."""
        wa = self.config["whatsapp"]
        payload = {
            "phone": str(wa["target_phone"]),
            "message": ("🔔 Test notification from the Stock Screener dashboard\n"
                        f"Time: {now_ist().strftime('%I:%M %p IST, %d-%b-%Y')}"),
        }
        try:
            resp = requests.post(wa["service_url"], json=payload, timeout=15)
            ok = resp.status_code == 200 and resp.json().get("success")
            return {"ok": bool(ok), "status": resp.status_code, "detail": resp.text[:300]}
        except requests.RequestException as exc:
            return {"ok": False, "status": 0,
                    "detail": f"WhatsApp service unreachable at {wa['service_url']}: {exc}"}


def _passes_filter(signal, flt):
    if flt in (None, "", "all"):
        return True
    if signal is None:
        return False
    if flt == "ready":
        return bool(signal.get("ready"))
    if flt == "ema":
        return bool(signal.get("ema_crossed_bb_middle"))
    if flt in ("breakout", "vwap"):
        return bool(signal.get("close_above_bb_upper"))
    return True


class Handler(BaseHTTPRequestHandler):
    data = None  # injected on the class before serving

    # quieten the default noisy logging; route through our logger instead.
    def log_message(self, fmt, *args):  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ----- helpers ----------------------------------------------------------- #

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path):
        # default document
        rel = path.lstrip("/")
        if rel == "" or rel == "/":
            rel = "index.html"
        target = (WEB_DIR / rel).resolve()
        # prevent path traversal outside web dir
        if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
            self._send_json({"error": "forbidden"}, 403)
            return
        if not target.is_file():
            self._send_json({"error": "not found", "path": rel}, 404)
            return
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ----- routing ----------------------------------------------------------- #

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path == "/api/health":
                self._send_json({"status": "ok", "time": now_ist().strftime("%H:%M:%S")})
            elif path == "/api/dashboard":
                limit = int(qs.get("limit", ["10000"])[0])
                flt = qs.get("filter", ["all"])[0]
                summary = qs.get("summary", ["0"])[0] in ("1", "true", "yes")
                self._send_json(self.data.dashboard(limit=limit, flt=flt, summary=summary))
            elif path.startswith("/api/candles/"):
                symbol = path[len("/api/candles/"):].strip("/").upper()
                payload = self.data.symbol_series(symbol)
                if payload is None:
                    self._send_json({"error": "no data for symbol", "symbol": symbol}, 404)
                else:
                    self._send_json(payload)
            elif path == "/api/alerts":
                limit = int(qs.get("limit", ["50"])[0])
                self._send_json({"alerts": self.data.alerts(limit)})
            elif path == "/api/stats":
                self._send_json(self.data.stats())
            elif path == "/api/whatsapp/status":
                self._send_json(self.data.wa_status())
            elif path == "/api/whatsapp/qr":
                self._send_json(self.data.wa_get("/qr"))
            elif path == "/api/whatsapp/history":
                self._send_json(self.data.wa_history(100))
            elif path.startswith("/api/"):
                self._send_json({"error": "unknown endpoint", "path": path}, 404)
            else:
                self._send_static(path)
        except Exception as exc:  # noqa: BLE001
            log.exception("Request failed: %s", path)
            self._send_json({"error": str(exc)}, 500)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/send-test":
                self._send_json(self.data.send_test())
            elif parsed.path == "/api/whatsapp/send-ready":
                self._send_json(self.data.send_ready())
            else:
                self._send_json({"error": "unknown endpoint", "path": parsed.path}, 404)
        except Exception as exc:  # noqa: BLE001
            log.exception("POST failed: %s", parsed.path)
            self._send_json({"error": str(exc)}, 500)


def main():
    port = 8000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    Handler.data = DashboardData()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("Dashboard server running at http://localhost:%d  (serving %s)", port, WEB_DIR)
    print(f"\n  Stock Screener dashboard:  http://localhost:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down dashboard server")
        server.shutdown()


if __name__ == "__main__":
    main()
