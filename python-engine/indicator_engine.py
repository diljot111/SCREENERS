"""
indicator_engine.py
===================
Technical indicator calculations on the daily timeframe.

Indicators (Bollinger Bands, EMA) are computed directly with pandas/numpy.
This is intentional: it is mathematically identical to pandas_ta but does not
break against current numpy/pandas releases. pandas_ta remains an optional
dependency if you prefer it (see README).

All functions operate on a list of candle dicts ordered ascending by date,
each with keys: date, open, high, low, close, volume.
"""

import logging

import numpy as np
import pandas as pd

log = logging.getLogger("screener.indicators")


def candles_to_df(candles):
    """Convert a list of candle dicts to a pandas DataFrame (ascending by date)."""
    df = pd.DataFrame(candles)
    if df.empty:
        return df
    df = df.sort_values("date").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def ema(series, period):
    """Exponential moving average. Returns a pandas Series."""
    return series.ewm(span=period, adjust=False).mean()


def bollinger_bands(series, period=20, std=2):
    """
    Returns (middle, upper, lower) as pandas Series.
    Middle = SMA(period); bands = middle +/- std * population stddev (ddof=0),
    matching the standard TradingView / pandas_ta Bollinger definition.
    """
    middle = series.rolling(window=period, min_periods=period).mean()
    deviation = series.rolling(window=period, min_periods=period).std(ddof=0)
    upper = middle + std * deviation
    lower = middle - std * deviation
    return middle, upper, lower


def compute_indicators(candles, bb_period=20, bb_std=2, ema_period=9):
    """
    Compute the latest-row indicator values from a candle list.

    Returns a dict with ema9, bb_middle, bb_upper, bb_lower (latest closed/forming
    row), or None if there is insufficient history.
    """
    df = candles_to_df(candles)
    if df.empty or len(df) < bb_period:
        return None

    close = df["close"]
    ema_series = ema(close, ema_period)
    mid, upper, lower = bollinger_bands(close, bb_period, bb_std)

    latest = {
        "ema9": _safe_last(ema_series),
        "bb_middle": _safe_last(mid),
        "bb_upper": _safe_last(upper),
        "bb_lower": _safe_last(lower),
    }
    if any(v is None or np.isnan(v) for v in latest.values()):
        return None
    return latest


def compute_yesterday_indicators(candles, bb_period=20, bb_std=2, ema_period=9):
    """
    Compute indicator values for the LAST CLOSED candle (yesterday).

    Used at market open to cache the "previous" values for crossover detection.
    Also stores vwap_proxy = yesterday's typical price (H+L+C)/3, a reasonable
    stand-in for the previous session's VWAP when intraday tick history is gone.
    """
    df = candles_to_df(candles)
    if df.empty or len(df) < bb_period:
        return None

    vals = compute_indicators(candles, bb_period, bb_std, ema_period)
    if vals is None:
        return None

    last = df.iloc[-1]
    typical = (float(last["high"]) + float(last["low"]) + float(last["close"])) / 3.0
    vals["vwap_proxy"] = typical
    vals["date"] = str(last["date"])
    return vals


def compute_indicator_series(candles, bb_period=20, bb_std=2, ema_period=9):
    """
    Compute the FULL per-candle indicator series for charting.

    Returns a list of dicts (ascending by date), one per candle, each with the
    OHLCV plus ema9, bb_middle, bb_upper, bb_lower and vwap (the typical-price
    proxy (H+L+C)/3 — a stand-in for true intraday VWAP on historical bars).
    Indicator values are None for the warm-up rows where the window isn't full.
    """
    df = candles_to_df(candles)
    if df.empty:
        return []

    close = df["close"]
    ema_series = ema(close, ema_period)
    mid, upper, lower = bollinger_bands(close, bb_period, bb_std)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0

    out = []
    for i in range(len(df)):
        out.append({
            "date": str(df["date"].iloc[i]),
            "open": _f(df["open"].iloc[i]),
            "high": _f(df["high"].iloc[i]),
            "low": _f(df["low"].iloc[i]),
            "close": _f(df["close"].iloc[i]),
            "volume": _f(df["volume"].iloc[i]) or 0,
            "ema9": _f(ema_series.iloc[i]),
            "bb_middle": _f(mid.iloc[i]),
            "bb_upper": _f(upper.iloc[i]),
            "bb_lower": _f(lower.iloc[i]),
            "vwap": _f(typical.iloc[i]),
        })
    return out


def evaluate_daily_signal(series, opts=None):
    """
    Evaluate a DAILY (end-of-day) buy signal for the dashboard.

    The live screener's signal (screener.check_signal) is an *intraday* dual
    crossover that needs a live VWAP feed and rarely fires on closed daily bars.
    For an EOD dashboard we use the daily-appropriate equivalent — momentum
    turning up + a breakout — with optional quality filters so ``ready`` flags a
    smaller, higher-quality set instead of every penny stock poking its band.

    Base conditions:
        * uptrend   : 9 EMA above the BB middle line
        * breakout  : close above the BB upper band

    Quality filters (via ``opts``; all off by default for backward compat):
        * require_fresh_breakout      : breakout must be NEW today (crossed up)
        * min_price                   : close >= this (avoid penny stocks)
        * min_avg_volume              : 20-day avg volume >= this (liquidity)
        * require_volume_confirmation : today's volume >= 20-day avg volume

    Returns the signal dict (with the quality flags), or None if data is short.
    """
    opts = opts or {}
    min_price = opts.get("min_price", 0) or 0
    min_avg_volume = opts.get("min_avg_volume", 0) or 0
    require_fresh = bool(opts.get("require_fresh_breakout", False))
    require_vol_confirm = bool(opts.get("require_volume_confirmation", False))

    if len(series) < 2:
        return None
    prev, cur = series[-2], series[-1]
    needed = (prev["ema9"], prev["bb_middle"], prev["bb_upper"], prev["close"],
              cur["ema9"], cur["bb_middle"], cur["bb_upper"], cur["close"])
    if any(v is None for v in needed):
        return None

    ema_above = cur["ema9"] > cur["bb_middle"]
    close_above_upper = cur["close"] > cur["bb_upper"]
    ema_crossed = (prev["ema9"] <= prev["bb_middle"]) and ema_above
    close_crossed_upper = (prev["close"] <= prev["bb_upper"]) and close_above_upper

    # liquidity / volume context from the last 20 bars
    vols = [r["volume"] for r in series[-20:] if r.get("volume")]
    avg_volume = sum(vols) / len(vols) if vols else 0
    today_volume = cur.get("volume") or 0
    price = cur["close"]

    breakout_ok = close_crossed_upper if require_fresh else close_above_upper
    price_ok = price >= min_price
    liquidity_ok = avg_volume >= min_avg_volume
    volume_ok = (not require_vol_confirm) or (today_volume >= avg_volume and avg_volume > 0)

    ready = ema_above and breakout_ok and price_ok and liquidity_ok and volume_ok

    return {
        "ema_above_bb_middle": ema_above,
        "close_above_bb_upper": close_above_upper,
        "ema_crossed_bb_middle": ema_crossed,
        "close_crossed_bb_upper": close_crossed_upper,
        "breakout": close_above_upper,
        "ready": ready,
        "price_ok": price_ok,
        "liquidity_ok": liquidity_ok,
        "volume_ok": volume_ok,
        "avg_volume": round(avg_volume),
        "today_volume": today_volume,
        "price": cur["close"],
        "ema9": cur["ema9"],
        "bb_middle": cur["bb_middle"],
        "bb_upper": cur["bb_upper"],
        "bb_lower": cur["bb_lower"],
        "vwap": cur["vwap"],
        "date": cur["date"],
    }


def _f(val):
    """Coerce a pandas/numpy scalar to a plain float, or None for NaN/None."""
    if val is None or pd.isna(val):
        return None
    return float(val)


def _safe_last(series):
    if series is None or len(series) == 0:
        return None
    val = series.iloc[-1]
    if pd.isna(val):
        return None
    return float(val)


def append_today_to_history(history_candles, today_candle, today_date):
    """
    Append the forming candle to the closed-candle history as the latest row.

    history_candles: list of closed candle dicts
    today_candle: forming candle dict from CandleBuilder (open/high/low/close/volume)
    Returns a new list (history is not mutated).
    """
    row = {
        "date": today_date,
        "open": today_candle.get("open"),
        "high": today_candle.get("high"),
        "low": today_candle.get("low"),
        "close": today_candle.get("close"),
        "volume": today_candle.get("volume", 0),
    }
    # Guard: if today's date already present in history (shouldn't be), replace it.
    filtered = [c for c in history_candles if str(c["date"]) != str(today_date)]
    return filtered + [row]
