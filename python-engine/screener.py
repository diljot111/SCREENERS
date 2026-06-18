"""
screener.py
===========
Applies the crossover filter to every symbol once per scan cycle.

Filter (daily timeframe, both conditions required):
  1. 9 EMA crossed ABOVE Bollinger Band middle line, AND
  2. VWAP crossed ABOVE Bollinger Band upper line.

A crossover means: yesterday the value was <= the band, and right now it is >
the band. Yesterday's indicator values come from the pre-computed cache; today's
forming values are recomputed every cycle from history + the live candle.
"""

import logging

from indicator_engine import append_today_to_history, compute_indicators
from db import now_ist

log = logging.getLogger("screener.screener")


def check_signal(history_candles, today_candle, current_vwap, yesterday_values,
                 today_date, bb_period=20, bb_std=2, ema_period=9):
    """
    Evaluate the filter for one symbol.

    Returns a dict of the signal's indicator values if BOTH crossover conditions
    are met, otherwise None.
    """
    if not yesterday_values or today_candle is None or current_vwap is None:
        return None

    prev_ema9 = yesterday_values.get("ema9")
    prev_bb_middle = yesterday_values.get("bb_middle")
    prev_bb_upper = yesterday_values.get("bb_upper")
    prev_vwap = yesterday_values.get("vwap_proxy")
    if None in (prev_ema9, prev_bb_middle, prev_bb_upper, prev_vwap):
        return None

    full = append_today_to_history(history_candles, today_candle, today_date)
    current = compute_indicators(full, bb_period, bb_std, ema_period)
    if current is None:
        return None

    current_ema9 = current["ema9"]
    current_bb_middle = current["bb_middle"]
    current_bb_upper = current["bb_upper"]

    # Condition 1: 9 EMA crossed above BB middle.
    ema_crossed_bb_mid = (prev_ema9 <= prev_bb_middle) and (current_ema9 > current_bb_middle)

    # Condition 2: VWAP crossed above BB upper.
    vwap_crossed_bb_upper = (prev_vwap <= prev_bb_upper) and (current_vwap > current_bb_upper)

    if ema_crossed_bb_mid and vwap_crossed_bb_upper:
        return {
            "ema9": current_ema9,
            "bb_middle": current_bb_middle,
            "bb_upper": current_bb_upper,
            "bb_lower": current["bb_lower"],
            "vwap": current_vwap,
            "price": today_candle.get("close"),
        }
    return None


class Screener:
    """Runs one scan cycle across all symbols and returns the matches."""

    def __init__(self, db, candle_builder, symbols, config):
        self.db = db
        self.candles = candle_builder
        self.symbols = symbols
        self.cfg = config["screener"]
        self.bb_period = self.cfg["bollinger_period"]
        self.bb_std = self.cfg["bollinger_std"]
        self.ema_period = self.cfg["ema_period"]
        self.batch_size = self.cfg.get("db_batch_size", 500)
        # symbol -> name lookup for nicer alerts
        self.name_by_symbol = {s["symbol"]: s.get("name", s["symbol"]) for s in symbols}
        self._yesterday_cache = {}

    def load_yesterday_cache(self):
        """Load the pre-computed previous-day indicator values from the DB."""
        self._yesterday_cache = self.db.get_all_indicator_cache()
        log.info("Loaded yesterday indicator cache for %d symbols",
                 len(self._yesterday_cache))

    def scan(self):
        """
        Run one scan cycle. Returns a list of match dicts:
          {symbol, name, price, ema9, bb_middle, bb_upper, bb_lower, vwap}
        Processes symbols in batches to keep memory bounded.
        """
        if not self._yesterday_cache:
            self.load_yesterday_cache()

        today_date = now_ist().strftime("%Y-%m-%d")
        matches = []
        scanned = 0

        for i in range(0, len(self.symbols), self.batch_size):
            batch = self.symbols[i:i + self.batch_size]
            for sym in batch:
                symbol = sym["symbol"]
                today_candle = self.candles.get(symbol)
                if today_candle is None:
                    continue
                scanned += 1

                yesterday = self._yesterday_cache.get(symbol)
                if not yesterday:
                    continue

                history = self.db.get_recent_candles(
                    symbol, limit=self.cfg.get("history_candles_required", 25)
                )
                if len(history) < self.bb_period:
                    continue

                current_vwap = self.candles.get_vwap(symbol)

                try:
                    signal = check_signal(
                        history, today_candle, current_vwap, yesterday, today_date,
                        self.bb_period, self.bb_std, self.ema_period,
                    )
                except Exception:  # noqa: BLE001 - never let one bad symbol kill the scan
                    log.exception("Error evaluating signal for %s", symbol)
                    continue

                if signal:
                    signal["symbol"] = symbol
                    signal["name"] = self.name_by_symbol.get(symbol, symbol)
                    matches.append(signal)

        log.info("Scan complete: scanned=%d matches=%d", scanned, len(matches))
        return matches
