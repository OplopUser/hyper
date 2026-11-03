"""
bot/strategy.py — EMA crossover strategy with RSI, HTF trend, and volatility filters.

Strategy logic (v2)
-------------------
1. Compute fast EMA (9) and slow EMA (21) on close prices.
2. Compute RSI (14) — only trade in the neutral 30-70 zone.
3. Compute ATR (14) for dynamic stop-loss / take-profit.

Filters (new):
4. Higher-timeframe (HTF) trend filter — only trade in the direction
   of the 4h EMA. If 4h price > 4h EMA(21), only take longs. Vice versa.
5. Volatility filter — skip signals when ATR is in the bottom 20th
   percentile (market is dead / ranging) or above the 95th percentile
   (chaos / news event).

Signal rules:
  LONG  : fast EMA crosses ABOVE slow EMA
          AND  30 < RSI < 70
          AND  4h close > 4h EMA (uptrend)
          AND  ATR is in normal range
  SHORT : fast EMA crosses BELOW slow EMA
          AND  30 < RSI < 70
          AND  4h close < 4h EMA (downtrend)
          AND  ATR is in normal range
  NONE  : any filter blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, Optional

import numpy as np
import pandas as pd
import ta

import config
from utils.logger import get_logger, log_signal

log = get_logger(__name__)


# Per-symbol cache of the last bar timestamp we already emitted a signal for.
# Prevents re-firing on the same (still-forming or closed) candle across
# successive 60s polling iterations.
_last_signal_bar: Dict[str, pd.Timestamp] = {}


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class Direction(Enum):
    LONG  = auto()
    SHORT = auto()
    NONE  = auto()


@dataclass
class Signal:
    """Result of one strategy evaluation cycle."""
    direction: Direction

    # Latest indicator values
    fast_ema:  float = 0.0
    slow_ema:  float = 0.0
    rsi:       float = 50.0
    atr:       float = 0.0
    close:     float = 0.0

    # Filter states (for logging / dashboard)
    htf_trend: str = "unknown"       # "bullish", "bearish", "unknown"
    vol_regime: str = "normal"       # "low", "normal", "high"
    blocked_by: Optional[str] = None # which filter blocked the signal

    # Pre-calculated stop-loss and take-profit prices
    stop_loss:   Optional[float] = None
    take_profit: Optional[float] = None

    @property
    def is_actionable(self) -> bool:
        return self.direction != Direction.NONE


# ---------------------------------------------------------------------------
# Indicator computation
# ---------------------------------------------------------------------------

def _compute_indicators(df: pd.DataFrame, cfg=None) -> pd.DataFrame:
    """Add EMA, RSI, and ATR columns to a OHLCV DataFrame."""
    cfg = cfg or config.STRATEGY

    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    df["ema_fast"] = close.ewm(span=cfg.ema_fast, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=cfg.ema_slow, adjust=False).mean()

    rsi_indicator = ta.momentum.RSIIndicator(close=close, window=cfg.rsi_period)
    df["rsi"] = rsi_indicator.rsi()

    atr_indicator = ta.volatility.AverageTrueRange(
        high=high, low=low, close=close, window=cfg.atr_period
    )
    df["atr"] = atr_indicator.average_true_range()

    return df


def _detect_crossover(
    prev_fast: float, prev_slow: float,
    curr_fast: float, curr_slow: float,
) -> Direction:
    """Detect an EMA crossover on the most recent candle."""
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        return Direction.LONG
    if prev_fast >= prev_slow and curr_fast < curr_slow:
        return Direction.SHORT
    return Direction.NONE


def _sl_tp(direction: Direction, entry_price: float, atr: float, cfg=None):
    cfg = cfg or config.STRATEGY
    sl_delta = atr * cfg.atr_sl_multiplier
    tp_delta = atr * cfg.atr_tp_multiplier
    if direction == Direction.LONG:
        return (entry_price - sl_delta, entry_price + tp_delta)
    else:
        return (entry_price + sl_delta, entry_price - tp_delta)


# ---------------------------------------------------------------------------
# HTF trend filter
# ---------------------------------------------------------------------------

def compute_htf_trend(htf_df: pd.DataFrame, cfg=None) -> str:
    """
    Determine the higher-timeframe trend direction.

    Parameters
    ----------
    htf_df : OHLCV DataFrame at the higher timeframe (e.g. 4h candles).

    Returns
    -------
    "bullish"  if current close > HTF EMA
    "bearish"  if current close < HTF EMA
    "unknown"  if not enough data
    """
    cfg = cfg or config.STRATEGY
    if htf_df.empty or len(htf_df) < cfg.htf_ema_period + 1:
        return "unknown"

    close = htf_df["close"].astype(float)
    htf_ema = close.ewm(span=cfg.htf_ema_period, adjust=False).mean()

    latest_close = float(close.iloc[-1])
    latest_ema   = float(htf_ema.iloc[-1])

    if latest_close > latest_ema:
        return "bullish"
    elif latest_close < latest_ema:
        return "bearish"
    return "unknown"


# ---------------------------------------------------------------------------
# Volatility filter
# ---------------------------------------------------------------------------

def compute_vol_regime(df: pd.DataFrame, cfg=None) -> str:
    """
    Classify current volatility as "low", "normal", or "high" based on
    where current ATR sits relative to recent ATR distribution.

    Parameters
    ----------
    df : DataFrame that already has an "atr" column computed.

    Returns
    -------
    "low"    if ATR < vol_low_percentile of recent values
    "high"   if ATR > vol_high_percentile of recent values
    "normal" otherwise
    """
    cfg = cfg or config.STRATEGY
    atr_series = df["atr"].dropna()

    if len(atr_series) < cfg.vol_atr_lookback:
        return "normal"  # not enough data to judge

    recent_atr = atr_series.tail(cfg.vol_atr_lookback)
    current_atr = float(atr_series.iloc[-1])

    low_threshold  = float(np.percentile(recent_atr, cfg.vol_low_percentile))
    high_threshold = float(np.percentile(recent_atr, cfg.vol_high_percentile))

    if current_atr < low_threshold:
        return "low"
    elif current_atr > high_threshold:
        return "high"
    return "normal"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_signal(
    df: pd.DataFrame,
    htf_df: Optional[pd.DataFrame] = None,
    symbol: str = "",
    cfg=None,
    dedupe: bool = True,
) -> Signal:
    """
    Evaluate the full strategy on candle data with all filters.

    The last row of *df* is treated as the still-forming candle and never
    read: every decision uses closed candles only (df.iloc[:-1]).

    Parameters
    ----------
    df      : Primary timeframe OHLCV DataFrame (e.g. 15m).
    htf_df  : Higher timeframe OHLCV DataFrame (e.g. 4h). Optional.
    symbol  : Coin symbol for logging.
    cfg     : StrategyConfig instance.
    dedupe  : Skip a closed bar this symbol already produced a signal on
              (live polling). The backtester visits each bar once and
              passes False so runs don't share state.

    Returns
    -------
    Signal with direction LONG, SHORT, or NONE.
    """
    cfg = cfg or config.STRATEGY
    symbol = symbol or config.SYMBOL

    # --- Minimum data check ---
    if len(df) < cfg.min_candles:
        log.debug("[%s] Not enough candles (%d < %d).", symbol, len(df), cfg.min_candles)
        return Signal(direction=Direction.NONE)

    df = _compute_indicators(df.copy(), cfg)
    df = df.dropna(subset=["ema_fast", "ema_slow", "rsi", "atr"])
    # Need at least 3 rows: [-3] = prior closed, [-2] = last closed, [-1] = forming
    if len(df) < 3:
        return Signal(direction=Direction.NONE)

    # Evaluate on the LAST CLOSED candle, not the still-forming one.
    # df.iloc[-1] is the in-progress bar — its EMAs flicker intra-bar and
    # cause the same crossover to re-fire every minute. By using iloc[-2]
    # as curr and iloc[-3] as prev, we only signal once per closed bar.
    prev = df.iloc[-3]
    curr = df.iloc[-2]

    curr_fast = float(curr["ema_fast"])
    curr_slow = float(curr["ema_slow"])
    prev_fast = float(prev["ema_fast"])
    prev_slow = float(prev["ema_slow"])
    rsi       = float(curr["rsi"])
    atr       = float(curr["atr"])
    close     = float(curr["close"])

    # Per-bar de-dupe: if we already processed this closed bar for this
    # symbol, return no-signal without re-running the filters.
    curr_ts = curr.get("timestamp") if "timestamp" in curr else None
    last_ts = _last_signal_bar.get(symbol)
    if dedupe and curr_ts is not None and last_ts is not None and curr_ts == last_ts:
        log.debug("[%s] Already evaluated bar %s — skipping", symbol, curr_ts)
        return Signal(
            direction=Direction.NONE,
            fast_ema=curr_fast, slow_ema=curr_slow,
            rsi=rsi, atr=atr, close=close,
            blocked_by="already_signaled_this_bar",
        )

    # --- HTF trend filter ---
    htf_trend = "unknown"
    if cfg.htf_enabled and htf_df is not None:
        htf_trend = compute_htf_trend(htf_df, cfg)
        log.debug("[%s] HTF trend: %s", symbol, htf_trend)

    # --- Volatility filter ---
    vol_regime = "normal"
    if cfg.vol_filter_enabled:
        # Closed candles only — the forming candle's ATR moves intra-bar.
        vol_regime = compute_vol_regime(df.iloc[:-1], cfg)
        log.debug("[%s] Vol regime: %s (ATR=%.4f)", symbol, vol_regime, atr)

    # Build a "no signal" template with current indicator values
    def _no_signal(blocked_by: Optional[str] = None) -> Signal:
        return Signal(
            direction=Direction.NONE,
            fast_ema=curr_fast, slow_ema=curr_slow,
            rsi=rsi, atr=atr, close=close,
            htf_trend=htf_trend, vol_regime=vol_regime,
            blocked_by=blocked_by,
        )

    # --- EMA crossover ---
    direction = _detect_crossover(prev_fast, prev_slow, curr_fast, curr_slow)
    if direction == Direction.NONE:
        return _no_signal()

    # --- RSI filter ---
    if not (cfg.rsi_oversold < rsi < cfg.rsi_overbought):
        log.debug("[%s] RSI filter blocked %s: rsi=%.2f", symbol, direction.name, rsi)
        return _no_signal(blocked_by="rsi")

    # --- HTF trend filter ---
    if cfg.htf_enabled and htf_trend != "unknown":
        if direction == Direction.LONG and htf_trend == "bearish":
            log.info("[%s] HTF trend filter blocked LONG (4h trend is bearish)", symbol)
            return _no_signal(blocked_by="htf_trend")
        if direction == Direction.SHORT and htf_trend == "bullish":
            log.info("[%s] HTF trend filter blocked SHORT (4h trend is bullish)", symbol)
            return _no_signal(blocked_by="htf_trend")

    # --- Volatility filter ---
    if cfg.vol_filter_enabled and vol_regime != "normal":
        reason = "too quiet (ranging)" if vol_regime == "low" else "too volatile (chaos)"
        log.info("[%s] Volatility filter blocked %s: %s", symbol, direction.name, reason)
        return _no_signal(blocked_by=f"vol_{vol_regime}")

    # --- All filters passed: signal confirmed ---
    stop_loss, take_profit = _sl_tp(direction, close, atr, cfg)

    # Mark this bar as signaled so we don't re-emit on the next poll
    if dedupe and curr_ts is not None:
        _last_signal_bar[symbol] = curr_ts

    log_signal(
        symbol=symbol,
        direction=direction.name,
        fast_ema=curr_fast,
        slow_ema=curr_slow,
        rsi=rsi,
        atr=atr,
    )

    return Signal(
        direction=direction,
        fast_ema=curr_fast, slow_ema=curr_slow,
        rsi=rsi, atr=atr, close=close,
        htf_trend=htf_trend, vol_regime=vol_regime,
        stop_loss=stop_loss, take_profit=take_profit,
    )


def adopted_sl_tp(
    df: pd.DataFrame,
    direction: Direction,
    entry_price: float,
    cfg=None,
) -> Optional[Signal]:
    """
    Rebuild SL/TP for a position that was already open when the bot started.

    The original signal's ATR isn't persisted, so this uses the ATR of the
    last closed candle and anchors the levels on the exchange's entry price
    (same multipliers as compute_signal). Returns None if there isn't
    enough candle data.
    """
    cfg = cfg or config.STRATEGY
    if df.empty or direction == Direction.NONE:
        return None
    df = _compute_indicators(df.copy(), cfg).dropna(subset=["atr"])
    if len(df) < 2:
        return None
    atr = float(df.iloc[-2]["atr"])
    if atr <= 0:
        return None
    stop_loss, take_profit = _sl_tp(direction, entry_price, atr, cfg)
    return Signal(
        direction=direction,
        atr=atr, close=entry_price,
        stop_loss=stop_loss, take_profit=take_profit,
    )
