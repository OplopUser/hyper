"""
config.py — Centralised configuration for the Hyperliquid trading bot.

All values are read from environment variables (via .env) with safe defaults.
Import this module everywhere instead of reading os.environ directly.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

# Load .env from the project root (or any parent directory)
load_dotenv()


def _require_env(key: str) -> str:
    """Raise a clear error when a mandatory env var is missing."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example to .env and fill in your values."
        )
    return value


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    return float(raw) if raw else default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    return int(raw) if raw else default


def _env_bool(key: str, default: bool) -> bool:
    """Parse a boolean env var. Unset, empty or unrecognised values give *default*."""
    raw = os.getenv(key, "").strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    return default


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

# Parsed only here. main.py, bot/, data/ and dashboard.py all read
# config.USE_TESTNET / config.HYPERLIQUID_API_URL instead of the raw env var.
USE_TESTNET: bool = _env_bool("USE_TESTNET", default=True)

# Base URLs — set automatically based on USE_TESTNET
HYPERLIQUID_API_URL: str = (
    "https://api.hyperliquid-testnet.xyz" if USE_TESTNET
    else "https://api.hyperliquid.xyz"
)

# ---------------------------------------------------------------------------
# Wallet / Auth  (lazy — only validated when the bot actually starts)
# ---------------------------------------------------------------------------

def get_private_key() -> str:
    return _require_env("PRIVATE_KEY")

def get_api_wallet_address() -> str:
    return _require_env("API_WALLET_ADDRESS")

def get_account_address() -> str:
    """Main wallet address that holds funds. Info queries use this address."""
    return _require_env("ACCOUNT_ADDRESS")

def get_account_address_optional() -> str:
    """ACCOUNT_ADDRESS, or "" if unset (the read-only dashboard runs without it)."""
    return os.getenv("ACCOUNT_ADDRESS", "").strip()

# ---------------------------------------------------------------------------
# AWS / Reservoir
# ---------------------------------------------------------------------------

AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_DEFAULT_REGION: str = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

# Hydromancer Reservoir S3 bucket (requester-pays)
RESERVOIR_BUCKET: str = "hyperliquid-reservoir"

# ---------------------------------------------------------------------------
# Trading universe
# ---------------------------------------------------------------------------

# Multiple symbols: comma-separated in .env, e.g. "ETH,SOL,BTC"
SYMBOLS: list[str] = [
    s.strip() for s in os.getenv("SYMBOLS", os.getenv("SYMBOL", "ETH,SOL")).split(",")
]
# Keep SYMBOL for backward compat
SYMBOL: str = SYMBOLS[0]

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    # EMA crossover periods
    ema_fast: int = field(default_factory=lambda: _env_int("EMA_FAST", 9))
    ema_slow: int = field(default_factory=lambda: _env_int("EMA_SLOW", 21))

    # RSI settings
    rsi_period: int = field(default_factory=lambda: _env_int("RSI_PERIOD", 14))
    rsi_oversold: float = 30.0   # Below this → oversold (don't short)
    rsi_overbought: float = 70.0  # Above this → overbought (don't long)

    # ATR settings (used for stop-loss & take-profit)
    atr_period: int = field(default_factory=lambda: _env_int("ATR_PERIOD", 14))
    atr_sl_multiplier: float = 1.5   # Stop-loss  = entry ± 1.5 × ATR
    atr_tp_multiplier: float = 3.0   # Take-profit = entry ± 3.0 × ATR

    # Minimum number of closed candles required before generating a signal
    min_candles: int = 50

    # Candle timeframe for live data (used with Hyperliquid's candle endpoint)
    candle_interval: str = "15m"    # e.g. "1m", "5m", "15m", "1h", "4h", "1d"

    # Number of candles to fetch for analysis
    candle_lookback: int = 200

    # --- Higher timeframe trend filter ---
    # Only trade in the direction of this higher timeframe's EMA trend.
    # E.g. if htf_interval="4h" and HTF EMA is pointing up, only take longs.
    htf_enabled: bool = True
    htf_interval: str = "4h"        # Higher timeframe candle interval
    htf_ema_period: int = 21         # EMA period on the HTF
    htf_lookback: int = 100          # How many HTF candles to fetch

    # --- Volatility filter ---
    # Skip signals when ATR is abnormally low (ranging) or high (news chaos).
    # Expressed as percentile of recent ATR values.
    vol_filter_enabled: bool = True
    vol_atr_lookback: int = 100      # Number of candles to compute ATR distribution
    vol_low_percentile: float = 20.0   # Skip if ATR is below this percentile (too quiet)
    vol_high_percentile: float = 95.0  # Skip if ATR is above this percentile (too wild)


# ---------------------------------------------------------------------------
# Risk management parameters
# ---------------------------------------------------------------------------

@dataclass
class RiskConfig:
    # Maximum fraction of account equity risked per trade
    max_risk_per_trade: float = field(
        default_factory=lambda: _env_float("MAX_RISK_PER_TRADE", 0.02)
    )
    # Maximum number of simultaneous open positions
    max_positions: int = field(
        default_factory=lambda: _env_int("MAX_POSITIONS", 3)
    )
    # If account drawdown exceeds this fraction, halt trading
    max_drawdown_halt: float = 0.10   # 10 % drawdown halts the bot

    # Leverage to use when opening positions (set to 1 for no leverage)
    leverage: int = 1

    # Minimum order size in USD (to avoid dust orders)
    min_order_usd: float = 10.0


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

# Taker fee charged on each fill's notional (entry and exit are both market
# orders). 0.045 % is Hyperliquid's base-tier perps taker rate; volume tiers
# and staking discounts lower it. Override with `--taker-fee`.
BACKTEST_TAKER_FEE: float = 0.00045

# ---------------------------------------------------------------------------
# Bot loop
# ---------------------------------------------------------------------------

LOOP_INTERVAL: int = _env_int("LOOP_INTERVAL", 60)  # seconds between iterations

# Back-off after a failed iteration (e.g. equity fetch failed): wait
# ERROR_BACKOFF_INITIAL seconds, doubling on each consecutive failure up to
# ERROR_BACKOFF_MAX. Reset after the next successful iteration.
ERROR_BACKOFF_INITIAL: float = 5.0
ERROR_BACKOFF_MAX: float = 300.0

# Close every open position on SIGINT/SIGTERM. Off by default so that a
# restart (docker restart, Watchtower image update) leaves positions open;
# the next process re-adopts them on start-up.
CLOSE_ON_SHUTDOWN: bool = _env_bool("CLOSE_ON_SHUTDOWN", default=False)

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

# 0.0.0.0 so the Docker port mapping works; use 127.0.0.1 for local-only.
DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT: int = _env_int("DASHBOARD_PORT", 5050)

# ---------------------------------------------------------------------------
# Convenience singletons (imported directly by other modules)
# ---------------------------------------------------------------------------

STRATEGY = StrategyConfig()
RISK = RiskConfig()
