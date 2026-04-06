"""
utils/logger.py — Structured logging setup for the trading bot.

Features:
- Console output with colour-coded log levels (via a simple formatter)
- Optional file output that rotates daily, keeping 7 days of history
- A single `get_logger(name)` factory used by every module
- Trade-specific helpers: log_signal(), log_order(), log_position()
"""

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# ANSI colour codes for terminal output
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_BOLD  = "\033[1m"
_COLOURS: Dict[int, str] = {
    logging.DEBUG:    "\033[36m",   # Cyan
    logging.INFO:     "\033[32m",   # Green
    logging.WARNING:  "\033[33m",   # Yellow
    logging.ERROR:    "\033[31m",   # Red
    logging.CRITICAL: "\033[35m",   # Magenta
}


class _ColouredFormatter(logging.Formatter):
    """Adds ANSI colour to the log level name for terminal readability."""

    _FMT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    _DATE_FMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self, use_colour: bool = True) -> None:
        super().__init__(fmt=self._FMT, datefmt=self._DATE_FMT)
        self._use_colour = use_colour

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        if self._use_colour and record.levelno in _COLOURS:
            colour = _COLOURS[record.levelno]
            # Colour only the level name portion
            formatted = formatted.replace(
                record.levelname,
                f"{colour}{_BOLD}{record.levelname}{_RESET}",
                1,
            )
        return formatted


class _PlainFormatter(logging.Formatter):
    """Plain formatter without ANSI codes — used for file output."""

    _FMT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    _DATE_FMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self) -> None:
        super().__init__(fmt=self._FMT, datefmt=self._DATE_FMT)


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_root_configured = False
_log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")


def _ensure_root_configured() -> None:
    """
    Configure the root logger once — subsequent calls are no-ops.
    Called lazily so that importing this module doesn't immediately write files.
    """
    global _root_configured
    if _root_configured:
        return

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # --- Console handler ---
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(_ColouredFormatter(use_colour=sys.stdout.isatty()))
    root.addHandler(console)

    # --- File handler (rotates at midnight, keeps 7 days) ---
    os.makedirs(_log_dir, exist_ok=True)
    log_file = os.path.join(_log_dir, "bot.log")
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        backupCount=7,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_PlainFormatter())
    root.addHandler(file_handler)

    # Silence overly verbose third-party loggers
    for noisy_lib in ("urllib3", "botocore", "boto3", "s3transfer", "websocket"):
        logging.getLogger(noisy_lib).setLevel(logging.WARNING)

    _root_configured = True


def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger.  Call once per module:
        log = get_logger(__name__)
    """
    _ensure_root_configured()
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Structured trade-event helpers
# ---------------------------------------------------------------------------

_trade_log = None  # lazily initialised

def _get_trade_logger() -> logging.Logger:
    global _trade_log
    if _trade_log is None:
        _ensure_root_configured()
        _trade_log = logging.getLogger("trade")
    return _trade_log


def log_signal(
    symbol: str,
    direction: str,
    fast_ema: float,
    slow_ema: float,
    rsi: float,
    atr: float,
) -> None:
    """Log a strategy signal in a structured, easy-to-parse format."""
    _get_trade_logger().info(
        "[SIGNAL] symbol=%s direction=%s fast_ema=%.4f slow_ema=%.4f rsi=%.2f atr=%.4f",
        symbol, direction, fast_ema, slow_ema, rsi, atr,
    )


def log_order(
    action: str,
    symbol: str,
    side: str,
    size: float,
    price: Optional[float],
    order_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Log an order placement or cancellation."""
    extra_str = f" {extra}" if extra else ""
    _get_trade_logger().info(
        "[ORDER] action=%s symbol=%s side=%s size=%.6f price=%s order_id=%s%s",
        action, symbol, side, size,
        f"{price:.4f}" if price is not None else "MARKET",
        order_id or "n/a",
        extra_str,
    )


def log_position(
    symbol: str,
    side: str,
    size: float,
    entry_price: float,
    unrealised_pnl: float,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
) -> None:
    """Log the current state of an open position."""
    _get_trade_logger().info(
        "[POSITION] symbol=%s side=%s size=%.6f entry=%.4f upnl=%.4f sl=%s tp=%s",
        symbol, side, size, entry_price, unrealised_pnl,
        f"{stop_loss:.4f}" if stop_loss else "n/a",
        f"{take_profit:.4f}" if take_profit else "n/a",
    )


def log_risk_event(event: str, detail: str) -> None:
    """Log a risk-management event (drawdown halt, size cap, etc.)."""
    _get_trade_logger().warning("[RISK] event=%s detail=%s", event, detail)
