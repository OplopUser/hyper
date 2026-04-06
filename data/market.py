"""
data/market.py — Live market data via the Hyperliquid Info API.

Uses the official hyperliquid-python-sdk's Info class to fetch:
  - Real-time order book (L2)
  - Recent candles (OHLCV) from Hyperliquid's own endpoint
  - Mid-price for a coin
  - Funding rate
  - Current metadata (tick size, lot size, max leverage)
  - All open perp markets

This module is also the fallback data source for data/reservoir.py when
Hydromancer S3 is unavailable.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
from hyperliquid.info import Info

import config
from utils.logger import get_logger

log = get_logger(__name__)


class MarketData:
    """
    Wrapper around the Hyperliquid Info API.

    Keeps a single Info instance so the WebSocket connection (if any)
    is reused across calls.
    """

    def __init__(self) -> None:
        self._info = Info(
            base_url=config.HYPERLIQUID_API_URL,
            skip_ws=True,  # We don't need WS subscriptions for a polling bot
        )
        log.info(
            "MarketData connected to %s",
            "testnet" if config.USE_TESTNET else "mainnet",
        )

    # ------------------------------------------------------------------
    # Price data
    # ------------------------------------------------------------------

    def get_mid_price(self, coin: str) -> Optional[float]:
        """Return the current mid-price for *coin*, or None on error."""
        try:
            all_mids: Dict[str, str] = self._info.all_mids()
            raw = all_mids.get(coin)
            return float(raw) if raw is not None else None
        except Exception as exc:
            log.error("get_mid_price(%s) failed: %s", coin, exc)
            return None

    def get_all_mids(self) -> Dict[str, float]:
        """Return {coin: mid_price} for all perp markets."""
        try:
            return {k: float(v) for k, v in self._info.all_mids().items()}
        except Exception as exc:
            log.error("get_all_mids failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Candle / OHLCV data
    # ------------------------------------------------------------------

    def get_candles(
        self,
        coin: str,
        interval: str,
        start_time: Optional[int] = None,
        end_time:   Optional[int] = None,
        lookback_candles: int = 200,
    ) -> List[dict]:
        """
        Fetch OHLCV candles from Hyperliquid's REST endpoint.

        Parameters
        ----------
        coin             : e.g. "BTC"
        interval         : "1m", "5m", "15m", "1h", "4h", "1d"
        start_time       : Unix timestamp in milliseconds (optional)
        end_time         : Unix timestamp in milliseconds (optional)
        lookback_candles : If start/end not provided, how many recent candles to fetch.

        Returns list of dicts with keys: t, o, h, l, c, v (raw API format).
        """
        try:
            if start_time is None:
                interval_ms = _interval_to_ms(interval)
                end_time    = int(time.time() * 1000)
                start_time  = end_time - lookback_candles * interval_ms

            candles = self._info.candles_snapshot(
                name=coin,
                interval=interval,
                startTime=start_time,
                endTime=end_time,
            )
            log.debug("Fetched %d candles for %s/%s", len(candles), coin, interval)
            return candles
        except Exception as exc:
            log.error("get_candles(%s, %s) failed: %s", coin, interval, exc)
            return []

    def get_candles_df(
        self,
        coin: str,
        interval: str = None,
        lookback_candles: int = None,
    ) -> pd.DataFrame:
        """
        Convenience wrapper — returns candles as a normalised DataFrame.

        Used as the fallback function passed to data.reservoir.get_candles().
        """
        interval        = interval        or config.STRATEGY.candle_interval
        lookback_candles = lookback_candles or config.STRATEGY.candle_lookback

        raw = self.get_candles(coin, interval, lookback_candles=lookback_candles)
        if not raw:
            return pd.DataFrame()

        rows = []
        for c in raw:
            rows.append({
                "timestamp": pd.Timestamp(c["t"], unit="ms", tz="UTC"),
                "open":   float(c["o"]),
                "high":   float(c["h"]),
                "low":    float(c["l"]),
                "close":  float(c["c"]),
                "volume": float(c["v"]),
            })
        df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
        log.info(
            "Fetched %d %s candles for %s from live endpoint",
            len(df), interval, coin,
        )
        return df

    # ------------------------------------------------------------------
    # Order book
    # ------------------------------------------------------------------

    def get_l2_book(self, coin: str) -> Optional[dict]:
        """
        Return the L2 order book for *coin*.

        Response format (from SDK):
          {"coin": "BTC", "levels": [[bids], [asks]], "time": <ms>}
        Each bid/ask entry: {"px": str, "sz": str, "n": int}
        """
        try:
            return self._info.l2_snapshot(coin)
        except Exception as exc:
            log.error("get_l2_book(%s) failed: %s", coin, exc)
            return None

    def get_best_bid_ask(self, coin: str) -> Tuple[Optional[float], Optional[float]]:
        """Return (best_bid, best_ask) for *coin*."""
        book = self.get_l2_book(coin)
        if not book:
            return None, None
        try:
            bids = book["levels"][0]
            asks = book["levels"][1]
            best_bid = float(bids[0]["px"]) if bids else None
            best_ask = float(asks[0]["px"]) if asks else None
            return best_bid, best_ask
        except (IndexError, KeyError) as exc:
            log.error("Malformed order book for %s: %s", coin, exc)
            return None, None

    # ------------------------------------------------------------------
    # Market metadata
    # ------------------------------------------------------------------

    def get_meta(self) -> Optional[dict]:
        """Return full exchange metadata (perpetuals universe)."""
        try:
            return self._info.meta()
        except Exception as exc:
            log.error("get_meta failed: %s", exc)
            return None

    def get_coin_meta(self, coin: str) -> Optional[dict]:
        """
        Return metadata for a single coin (szDecimals, maxLeverage, etc.).
        """
        meta = self.get_meta()
        if not meta:
            return None
        for asset in meta.get("universe", []):
            if asset.get("name") == coin:
                return asset
        log.warning("Coin %s not found in exchange metadata", coin)
        return None

    def get_sz_decimals(self, coin: str) -> int:
        """Return the number of decimal places for order sizes."""
        meta = self.get_coin_meta(coin)
        return int(meta.get("szDecimals", 3)) if meta else 3

    # ------------------------------------------------------------------
    # Funding rate
    # ------------------------------------------------------------------

    def get_funding_rate(self, coin: str) -> Optional[float]:
        """Return the current 1-hour funding rate for *coin*."""
        try:
            data = self._info.meta_and_asset_ctxs()
            # data is [meta_dict, [asset_ctx, ...]]
            meta   = data[0]
            ctxs   = data[1]
            for asset, ctx in zip(meta.get("universe", []), ctxs):
                if asset.get("name") == coin:
                    return float(ctx.get("funding", 0))
            return None
        except Exception as exc:
            log.error("get_funding_rate(%s) failed: %s", coin, exc)
            return None

    # ------------------------------------------------------------------
    # User state (convenience delegation to avoid import cycles)
    # ------------------------------------------------------------------

    def get_user_state(self, address: str) -> Optional[dict]:
        """
        Return full user state (margins, positions, withdrawable).
        Delegated here so market data and account state share one Info instance.
        """
        try:
            return self._info.user_state(address)
        except Exception as exc:
            log.error("get_user_state(%s) failed: %s", address, exc)
            return None

    def get_open_orders(self, address: str) -> List[dict]:
        """Return all open orders for *address*."""
        try:
            return self._info.open_orders(address)
        except Exception as exc:
            log.error("get_open_orders(%s) failed: %s", address, exc)
            return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _interval_to_ms(interval: str) -> int:
    """Convert an interval string like '15m' or '1h' to milliseconds."""
    _MAP = {
        "1m":  60_000,
        "3m":  180_000,
        "5m":  300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h":  3_600_000,
        "2h":  7_200_000,
        "4h":  14_400_000,
        "6h":  21_600_000,
        "12h": 43_200_000,
        "1d":  86_400_000,
    }
    result = _MAP.get(interval.lower())
    if result is None:
        raise ValueError(
            f"Unknown interval '{interval}'. "
            f"Supported: {list(_MAP.keys())}"
        )
    return result
