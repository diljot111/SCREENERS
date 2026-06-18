"""
data_fetcher.py
===============
Angel One SmartAPI integration: authentication (API key + TOTP), historical
daily candle download (with a yfinance fallback), and the live tick WebSocket
(SmartWebSocketV2).

Prices from the Angel One feed and historical API are handled in rupees here;
the live feed delivers paise, which we convert (÷100) before use.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

import pyotp

from db import IST, now_ist

log = logging.getLogger("screener.data")

# Angel One feed: exchangeType 1 = NSE cash market (nse_cm).
NSE_EXCHANGE_TYPE = 1
# Subscription mode: 2 = Quote (gives OHLC + volume + LTP).
MODE_QUOTE = 2

INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)


class AngelOneClient:
    """Wraps SmartConnect for auth + historical data."""

    def __init__(self, config):
        ao = config["angel_one"]
        self.api_key = ao["api_key"]
        self.client_id = ao["client_id"]
        self.password = ao["password"]
        self.totp_secret = ao["totp_secret"]
        self.smart = None
        self.auth_token = None
        self.refresh_token = None
        self.feed_token = None

    def authenticate(self, max_retries=3, retry_gap=10):
        """Authenticate with Angel One. Retries on failure. Returns True/False."""
        from SmartApi import SmartConnect  # imported lazily so the module loads without it

        for attempt in range(1, max_retries + 1):
            try:
                self.smart = SmartConnect(api_key=self.api_key)
                totp = pyotp.TOTP(self.totp_secret).now()
                data = self.smart.generateSession(self.client_id, self.password, totp)
                if not data or not data.get("status"):
                    raise RuntimeError(f"generateSession failed: {data}")

                jwt = data["data"]["jwtToken"]
                # WebSocket V2 wants the raw token without the "Bearer " prefix.
                self.auth_token = jwt.replace("Bearer ", "").strip()
                self.refresh_token = data["data"].get("refreshToken")
                self.feed_token = self.smart.getfeedToken()
                log.info("Angel One authentication successful for %s", self.client_id)
                return True
            except Exception as exc:  # noqa: BLE001
                log.error("Angel One auth attempt %d/%d failed: %s",
                          attempt, max_retries, exc)
                if attempt < max_retries:
                    time.sleep(retry_gap)
        return False

    def fetch_historical(self, symbol, token, days=30):
        """
        Fetch the last `days` daily candles for a token. Returns a list of
        candle dicts {date, open, high, low, close, volume} ascending by date.
        Falls back to yfinance on failure.
        """
        to_date = now_ist()
        from_date = to_date - timedelta(days=int(days * 2) + 10)  # pad for weekends/holidays
        params = {
            "exchange": "NSE",
            "symboltoken": str(token),
            "interval": "ONE_DAY",
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.strftime("%Y-%m-%d %H:%M"),
        }
        try:
            res = self.smart.getCandleData(params)
            if res and res.get("status") and res.get("data"):
                candles = []
                for row in res["data"]:
                    # row: [timestamp, open, high, low, close, volume]
                    ts = row[0][:10]  # YYYY-MM-DD
                    candles.append({
                        "date": ts,
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": int(row[5]),
                    })
                if candles:
                    return candles[-days:]
            log.warning("Angel One historical empty for %s — trying yfinance", symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("Angel One historical failed for %s (%s) — trying yfinance",
                        symbol, exc)

        return self._yfinance_fallback(symbol, days)

    @staticmethod
    def _yfinance_fallback(symbol, days=30):
        try:
            import yfinance as yf

            ticker = yf.Ticker(f"{symbol}.NS")
            hist = ticker.history(period=f"{int(days * 2) + 15}d", interval="1d")
            if hist is None or hist.empty:
                log.warning("yfinance returned no data for %s", symbol)
                return []
            candles = []
            for idx, row in hist.iterrows():
                candles.append({
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]) if not _isnan(row["Volume"]) else 0,
                })
            return candles[-days:]
        except Exception as exc:  # noqa: BLE001
            log.error("yfinance fallback failed for %s: %s", symbol, exc)
            return []


def _isnan(v):
    try:
        return v != v
    except Exception:  # noqa: BLE001
        return False


class LiveFeed:
    """Manages the SmartWebSocketV2 live tick stream in a background thread."""

    def __init__(self, client, symbols, on_tick, config):
        self.client = client
        self.symbols = symbols
        self.on_tick = on_tick  # callable(symbol, price, cum_volume, o, h, l)
        self.batch_size = config["screener"].get("websocket_subscribe_batch", 1000)
        self.token_to_symbol = {str(s["token"]): s["symbol"] for s in symbols}
        self.sws = None
        self._thread = None
        self._running = False
        self._reconnect_attempts = 0
        self.correlation_id = "screener_ws"

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, name="LiveFeed", daemon=True)
        self._thread.start()
        log.info("Live feed thread started (%d symbols)", len(self.symbols))

    def _run(self):
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        while self._running:
            try:
                self.sws = SmartWebSocketV2(
                    self.client.auth_token,
                    self.client.api_key,
                    self.client.client_id,
                    self.client.feed_token,
                )
                self.sws.on_open = self._on_open
                self.sws.on_data = self._on_data
                self.sws.on_error = self._on_error
                self.sws.on_close = self._on_close
                # connect() blocks until the socket closes.
                self.sws.connect()
            except Exception as exc:  # noqa: BLE001
                log.error("WebSocket crashed: %s", exc)

            if not self._running:
                break

            # Exponential backoff reconnect.
            self._reconnect_attempts += 1
            backoff = min(60, 2 ** self._reconnect_attempts)
            log.warning("WebSocket disconnected — reconnecting in %ds (attempt %d)",
                        backoff, self._reconnect_attempts)
            time.sleep(backoff)

    def _on_open(self, wsapp):
        log.info("WebSocket open — subscribing in batches of %d", self.batch_size)
        self._reconnect_attempts = 0
        tokens = [str(s["token"]) for s in self.symbols]
        for i in range(0, len(tokens), self.batch_size):
            chunk = tokens[i:i + self.batch_size]
            token_list = [{"exchangeType": NSE_EXCHANGE_TYPE, "tokens": chunk}]
            try:
                self.sws.subscribe(self.correlation_id, MODE_QUOTE, token_list)
                log.info("Subscribed batch %d (%d tokens)",
                         i // self.batch_size + 1, len(chunk))
            except Exception as exc:  # noqa: BLE001
                log.error("Subscribe batch failed: %s", exc)
            time.sleep(0.5)  # gentle pacing between subscribe calls

    def _on_data(self, wsapp, message):
        try:
            if not isinstance(message, dict):
                return
            token = str(message.get("token", "")).strip().replace('"', "")
            symbol = self.token_to_symbol.get(token)
            if not symbol:
                return

            # Live feed prices are in paise — convert to rupees.
            ltp = _paise(message.get("last_traded_price"))
            if ltp is None:
                return
            cum_vol = message.get("volume_trade_for_the_day")
            o = _paise(message.get("open_price_of_the_day"))
            h = _paise(message.get("high_price_of_the_day"))
            low = _paise(message.get("low_price_of_the_day"))

            self.on_tick(symbol, ltp, cum_vol, o, h, low)
        except Exception:  # noqa: BLE001
            log.exception("Error handling tick")

    def _on_error(self, wsapp, error):
        log.error("WebSocket error: %s", error)

    def _on_close(self, wsapp):
        log.warning("WebSocket closed")

    def stop(self):
        self._running = False
        try:
            if self.sws:
                self.sws.close_connection()
        except Exception as exc:  # noqa: BLE001
            log.warning("Error closing WebSocket: %s", exc)
        log.info("Live feed stopped")


def _paise(value):
    """Convert a paise integer/float to rupees, or None."""
    if value is None:
        return None
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return None
