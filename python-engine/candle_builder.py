"""
candle_builder.py
=================
Maintains today's forming daily candle per symbol from live WebSocket ticks.

Thread-safety: ticks arrive on the Angel One WebSocket thread while the
screener reads candles on the APScheduler thread, so every access to the
shared `live_candles` dict is guarded by a lock.
"""

import logging
import threading

from db import now_ist

log = logging.getLogger("screener.candles")


class CandleBuilder:
    def __init__(self):
        self._lock = threading.Lock()
        # symbol -> forming candle dict
        self.live_candles = {}
        self._session_date = now_ist().strftime("%Y-%m-%d")

    def reset(self):
        """Clear all forming candles (called at market open / new day)."""
        with self._lock:
            self.live_candles.clear()
            self._session_date = now_ist().strftime("%Y-%m-%d")
        log.info("Candle builder reset for %s", self._session_date)

    def update(self, symbol, price, cumulative_volume, exchange_open=None,
               exchange_high=None, exchange_low=None, tick_volume=None):
        """
        Update the forming candle for `symbol` from one tick.

        price:              last traded price
        cumulative_volume:  total volume traded today (from exchange feed)
        exchange_open/high/low: optional OHLC straight from the feed snapshot
        tick_volume:        volume of THIS tick (used for incremental VWAP);
                            if None we derive it from the change in cumulative_volume.
        """
        if price is None or price <= 0:
            return

        with self._lock:
            candle = self.live_candles.get(symbol)
            if candle is None:
                candle = {
                    "open": exchange_open if exchange_open else price,
                    "high": exchange_high if exchange_high else price,
                    "low": exchange_low if exchange_low else price,
                    "close": price,
                    "volume": cumulative_volume or 0,
                    "vwap_numerator": 0.0,
                    "vwap_denominator": 0.0,
                    "last_cum_volume": 0,
                }
                self.live_candles[symbol] = candle

            # Derive the incremental volume for VWAP accumulation.
            if tick_volume is not None:
                inc_vol = max(tick_volume, 0)
            elif cumulative_volume is not None:
                inc_vol = max(cumulative_volume - candle["last_cum_volume"], 0)
            else:
                inc_vol = 0

            if cumulative_volume is not None:
                candle["last_cum_volume"] = cumulative_volume
                candle["volume"] = cumulative_volume

            candle["high"] = max(candle["high"], price,
                                 exchange_high if exchange_high else price)
            candle["low"] = min(candle["low"], price,
                                exchange_low if exchange_low else price)
            candle["close"] = price

            # VWAP accumulation: price * incremental volume.
            if inc_vol > 0:
                candle["vwap_numerator"] += price * inc_vol
                candle["vwap_denominator"] += inc_vol

    def get(self, symbol):
        """Return a copy of the forming candle for a symbol, or None."""
        with self._lock:
            candle = self.live_candles.get(symbol)
            return dict(candle) if candle else None

    def get_vwap(self, symbol):
        """Compute current VWAP for a symbol, guarding against divide-by-zero."""
        with self._lock:
            candle = self.live_candles.get(symbol)
            if not candle:
                return None
            denom = candle["vwap_denominator"]
            if denom <= 0:
                # No volume yet — fall back to last price (close).
                return candle["close"]
            return candle["vwap_numerator"] / denom

    def snapshot(self):
        """Return a shallow copy of all forming candles (for the screener loop)."""
        with self._lock:
            return {sym: dict(c) for sym, c in self.live_candles.items()}

    def count(self):
        with self._lock:
            return len(self.live_candles)
