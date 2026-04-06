"""
data/reservoir.py — Hydromancer Reservoir S3 historical data fetcher.

Hydromancer Reservoir stores Hyperliquid historical data in the S3 bucket
  s3://hyperliquid-reservoir
using "requester pays" billing (no Hyperliquid API key needed — only your own
AWS credentials).

Dataset layout (as documented by Hydromancer):
  fills/     — trades, liquidations, ADLs  (Parquet, partitioned by date)
  candles/   — 1-second OHLCV candles      (Parquet, partitioned by coin & date)
  snapshots/ — daily position / spot-balance snapshots (Parquet)

This module provides:
  - ReservoirClient  : low-level S3 reader returning pandas DataFrames
  - get_candles()    : convenience function used by the strategy module
  - get_fills()      : convenience function for trade history
  - get_snapshots()  : convenience function for account snapshots

Fallback:
  If S3 is unavailable (missing credentials, network error, etc.) every
  function falls back to Hyperliquid's native REST candle endpoint via
  data.market.MarketData.
"""

from __future__ import annotations

import io
import os
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import boto3
import pandas as pd
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError

import config
from utils.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# S3 bucket / key constants
# ---------------------------------------------------------------------------

_BUCKET = config.RESERVOIR_BUCKET
_CANDLES_PREFIX   = "candles"
_FILLS_PREFIX     = "fills"
_SNAPSHOTS_PREFIX = "snapshots"

# Requester-pays header required by the bucket
_REQ_PAYS = {"RequestPayer": "requester"}


# ---------------------------------------------------------------------------
# ReservoirClient
# ---------------------------------------------------------------------------

class ReservoirClient:
    """
    Thin wrapper around boto3 that reads Parquet files from
    s3://hyperliquid-reservoir with requester-pays billing.

    If AWS credentials are not configured the client falls back to
    anonymous access, which will fail on requester-pays objects — callers
    should catch ReservoirUnavailableError and use the fallback path.
    """

    def __init__(self) -> None:
        self._client = self._build_client()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def list_keys(self, prefix: str) -> List[str]:
        """Return all S3 object keys under *prefix* (handles pagination)."""
        keys: List[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(
                Bucket=_BUCKET,
                Prefix=prefix,
                **_REQ_PAYS,
            ):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
        except ClientError as exc:
            raise ReservoirUnavailableError(str(exc)) from exc
        return keys

    def read_parquet(self, key: str) -> pd.DataFrame:
        """Download a single Parquet object and return it as a DataFrame."""
        log.debug("Fetching s3://%s/%s", _BUCKET, key)
        try:
            resp = self._client.get_object(
                Bucket=_BUCKET, Key=key, **_REQ_PAYS
            )
            body = resp["Body"].read()
            return pd.read_parquet(io.BytesIO(body))
        except ClientError as exc:
            raise ReservoirUnavailableError(str(exc)) from exc

    def read_parquet_multi(self, keys: List[str]) -> pd.DataFrame:
        """Download and concatenate multiple Parquet objects."""
        if not keys:
            return pd.DataFrame()
        frames = [self.read_parquet(k) for k in keys]
        return pd.concat(frames, ignore_index=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_client():
        """
        Build a boto3 S3 client.
        Prefers explicit credentials from config; falls back to the default
        credential chain (env vars, ~/.aws/credentials, IAM role, etc.).
        """
        kwargs: dict = {"region_name": config.AWS_DEFAULT_REGION}

        if config.AWS_ACCESS_KEY_ID and config.AWS_SECRET_ACCESS_KEY:
            kwargs["aws_access_key_id"] = config.AWS_ACCESS_KEY_ID
            kwargs["aws_secret_access_key"] = config.AWS_SECRET_ACCESS_KEY
            log.debug("ReservoirClient: using credentials from config")
        else:
            log.debug("ReservoirClient: using default AWS credential chain")

        return boto3.client("s3", **kwargs)


class ReservoirUnavailableError(Exception):
    """Raised when the Reservoir S3 bucket cannot be accessed."""


# ---------------------------------------------------------------------------
# Candle helpers
# ---------------------------------------------------------------------------

def _candle_keys_for_range(
    coin: str,
    start: date,
    end: date,
) -> List[str]:
    """
    Generate expected S3 keys for 1-second candle Parquet files.

    Hydromancer partitions candles as:
      candles/<COIN>/YYYY-MM-DD.parquet
    """
    keys = []
    current = start
    while current <= end:
        keys.append(
            f"{_CANDLES_PREFIX}/{coin}/{current.isoformat()}.parquet"
        )
        current += timedelta(days=1)
    return keys


def get_candles(
    coin: str,
    days: int = 30,
    resample_to: Optional[str] = None,
    fallback_fn=None,
) -> pd.DataFrame:
    """
    Fetch OHLCV candle data for *coin* from Hydromancer Reservoir.

    Parameters
    ----------
    coin        : Trading symbol, e.g. "BTC".
    days        : Number of calendar days of history to fetch.
    resample_to : Optional pandas offset string to resample 1s candles,
                  e.g. "15min", "1h", "4h". If None, 1s candles are returned.
    fallback_fn : Callable() → pd.DataFrame used when S3 is unavailable.
                  Typically market.MarketData.get_candles_df().

    Returns
    -------
    DataFrame with columns: timestamp, open, high, low, close, volume
    Sorted ascending by timestamp.
    """
    end_date   = date.today()
    start_date = end_date - timedelta(days=days - 1)
    keys       = _candle_keys_for_range(coin, start_date, end_date)

    client = ReservoirClient()
    try:
        # Only request keys that actually exist to avoid 404 noise.
        existing = set(client.list_keys(f"{_CANDLES_PREFIX}/{coin}/"))
        keys     = [k for k in keys if k in existing]

        if not keys:
            log.warning(
                "No candle files found in Reservoir for %s (%s → %s)",
                coin, start_date, end_date,
            )
            if fallback_fn:
                log.info("Falling back to live candle endpoint for %s", coin)
                return fallback_fn()
            return pd.DataFrame()

        df = client.read_parquet_multi(keys)
        df = _normalise_candles(df)

        if resample_to:
            df = _resample_candles(df, resample_to)

        log.info(
            "Loaded %d candles for %s from Reservoir (%s → %s)",
            len(df), coin, start_date, end_date,
        )
        return df

    except ReservoirUnavailableError as exc:
        log.warning("Reservoir S3 unavailable (%s); using fallback.", exc)
        if fallback_fn:
            return fallback_fn()
        log.error("No fallback provided — returning empty DataFrame.")
        return pd.DataFrame()


def _normalise_candles(df: pd.DataFrame) -> pd.DataFrame:
    """
    Standardise column names and types from raw Reservoir Parquet files.

    Reservoir candle files may use various column names; we map them to
    a canonical schema: timestamp, open, high, low, close, volume.
    """
    # Common column name aliases seen in Hydromancer data
    rename_map = {
        "t": "timestamp", "time": "timestamp", "ts": "timestamp",
        "T": "timestamp",
        "o": "open",  "O": "open",
        "h": "high",  "H": "high",
        "l": "low",   "L": "low",
        "c": "close", "C": "close",
        "v": "volume","V": "volume",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing  = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Candle DataFrame is missing columns: {missing}. "
            f"Available columns: {list(df.columns)}"
        )

    # Ensure timestamp is UTC-aware datetime
    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        # May be Unix milliseconds (int) or a string
        try:
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        except Exception:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    elif df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")

    # Cast OHLCV to float
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)

    df = df.sort_values("timestamp").reset_index(drop=True)
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def _resample_candles(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample 1-second OHLCV data to a coarser timeframe."""
    df = df.set_index("timestamp")
    resampled = df.resample(rule).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna()
    return resampled.reset_index()


# ---------------------------------------------------------------------------
# Fills helpers
# ---------------------------------------------------------------------------

def get_fills(
    days: int = 7,
    coin: Optional[str] = None,
) -> pd.DataFrame:
    """
    Fetch trade fill history from Hydromancer Reservoir.

    Parameters
    ----------
    days : Number of days of fill history to retrieve.
    coin : If provided, filter to a single coin.

    Returns
    -------
    DataFrame with columns including: timestamp, coin, side, price, size,
    liquidation (bool), adl (bool).
    """
    end_date   = date.today()
    start_date = end_date - timedelta(days=days - 1)

    client = ReservoirClient()
    keys: List[str] = []
    try:
        available = set(client.list_keys(_FILLS_PREFIX + "/"))
        current   = start_date
        while current <= end_date:
            key = f"{_FILLS_PREFIX}/{current.isoformat()}.parquet"
            if key in available:
                keys.append(key)
            current += timedelta(days=1)

        if not keys:
            log.warning("No fill files found in Reservoir for range %s → %s",
                        start_date, end_date)
            return pd.DataFrame()

        df = client.read_parquet_multi(keys)
        df = _normalise_fills(df)

        if coin:
            df = df[df["coin"] == coin].copy()

        log.info("Loaded %d fills from Reservoir (%s → %s)", len(df), start_date, end_date)
        return df

    except ReservoirUnavailableError as exc:
        log.warning("Reservoir unavailable for fills: %s", exc)
        return pd.DataFrame()


def _normalise_fills(df: pd.DataFrame) -> pd.DataFrame:
    """Standardise fill DataFrame column names."""
    rename_map = {
        "time": "timestamp", "ts": "timestamp",
        "px":   "price",
        "sz":   "size",
        "dir":  "side",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    if "timestamp" in df.columns:
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            try:
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            except Exception:
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    for col in ("price", "size"):
        if col in df.columns:
            df[col] = df[col].astype(float)

    return df.sort_values("timestamp").reset_index(drop=True) if "timestamp" in df.columns else df


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def get_snapshots(days: int = 7) -> pd.DataFrame:
    """
    Fetch daily position / spot-balance snapshots from Hydromancer Reservoir.

    Returns
    -------
    DataFrame with daily account state rows.
    """
    end_date   = date.today()
    start_date = end_date - timedelta(days=days - 1)

    client = ReservoirClient()
    keys: List[str] = []
    try:
        available = set(client.list_keys(_SNAPSHOTS_PREFIX + "/"))
        current   = start_date
        while current <= end_date:
            key = f"{_SNAPSHOTS_PREFIX}/{current.isoformat()}.parquet"
            if key in available:
                keys.append(key)
            current += timedelta(days=1)

        if not keys:
            log.warning("No snapshot files found for %s → %s", start_date, end_date)
            return pd.DataFrame()

        df = client.read_parquet_multi(keys)
        log.info("Loaded %d snapshot rows from Reservoir", len(df))
        return df

    except ReservoirUnavailableError as exc:
        log.warning("Reservoir unavailable for snapshots: %s", exc)
        return pd.DataFrame()
