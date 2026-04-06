"""
main.py — Main entry point and bot loop for the Hyperliquid trading bot.

Run with:
    python main.py              # live trading
    python main.py --backtest   # run backtest first, then trade

The bot will:
  1. Validate environment / config on startup.
  2. Initialise all subsystems (market data, exchange client, risk manager).
  3. Enter the main loop for each symbol in SYMBOLS (every LOOP_INTERVAL seconds):
       a. Refresh account equity and check drawdown.
       b. Fetch primary timeframe candles + higher timeframe (4h) candles.
       c. Compute EMA crossover signal with RSI, HTF trend, and volatility filters.
       d. Manage existing positions (check SL/TP).
       e. Open new positions if signal is actionable and risk checks pass.
  4. On KeyboardInterrupt or fatal error: close open positions and exit.

Filters (v2):
  - HTF trend: only trade in the direction of the 4h EMA(21)
  - Volatility: skip when ATR is abnormally low (ranging) or high (chaos)
"""

from __future__ import annotations

import signal
import sys
import time
from functools import partial
from typing import Optional

import config
from bot.exchange import ExchangeClient
from bot.risk import PositionRecord, RiskManager
from bot.strategy import Direction, Signal, compute_signal
from data.market import MarketData
from utils.logger import get_logger, log_position, log_risk_event

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def validate_config() -> None:
    config.get_private_key()
    config.get_api_wallet_address()
    config.get_account_address()
    log.info("Config validated — symbols=%s  testnet=%s", config.SYMBOLS, config.USE_TESTNET)


# ---------------------------------------------------------------------------
# Single-symbol iteration
# ---------------------------------------------------------------------------

def run_iteration(
    symbol: str,
    market: MarketData,
    exchange: ExchangeClient,
    risk: RiskManager,
    equity: float,
) -> None:
    """Execute one full bot iteration for *symbol*."""
    try:
        _iteration(symbol, market, exchange, risk, equity)
    except Exception as exc:
        log.exception("Unhandled error in iteration for %s: %s", symbol, exc)


def _iteration(
    symbol: str,
    market: MarketData,
    exchange: ExchangeClient,
    risk: RiskManager,
    equity: float,
) -> None:
    # ------------------------------------------------------------------
    # 1. Fetch current price
    # ------------------------------------------------------------------
    mid_price = market.get_mid_price(symbol)
    if mid_price is None:
        log.warning("[%s] Could not get mid-price — skipping.", symbol)
        return
    log.debug("[%s] Mid price: $%.4f", symbol, mid_price)

    # ------------------------------------------------------------------
    # 2. Manage existing positions (stop-loss / take-profit)
    # ------------------------------------------------------------------
    _manage_existing_positions(symbol, mid_price, exchange, risk)

    # ------------------------------------------------------------------
    # 3. Check if we should look for a new entry
    # ------------------------------------------------------------------
    allowed, reason = risk.can_open_trade(symbol, equity)
    if not allowed:
        log.debug("[%s] New trade blocked: %s", symbol, reason)
        return

    # ------------------------------------------------------------------
    # 4. Fetch primary timeframe candles (live endpoint — skip S3 for speed)
    # ------------------------------------------------------------------
    df = market.get_candles_df(
        coin=symbol,
        interval=config.STRATEGY.candle_interval,
        lookback_candles=config.STRATEGY.candle_lookback,
    )
    if df.empty:
        log.warning("[%s] No candle data — skipping signal.", symbol)
        return

    # ------------------------------------------------------------------
    # 5. Fetch higher timeframe candles (for HTF trend filter)
    # ------------------------------------------------------------------
    htf_df = None
    if config.STRATEGY.htf_enabled:
        htf_df = market.get_candles_df(
            coin=symbol,
            interval=config.STRATEGY.htf_interval,
            lookback_candles=config.STRATEGY.htf_lookback,
        )
        if htf_df.empty:
            htf_df = None

    # ------------------------------------------------------------------
    # 6. Compute strategy signal (with all filters)
    # ------------------------------------------------------------------
    sig: Signal = compute_signal(df, htf_df=htf_df, symbol=symbol)

    if not sig.is_actionable:
        blocked = f" (blocked by {sig.blocked_by})" if sig.blocked_by else ""
        log.debug("[%s] No signal%s | ema_f=%.1f ema_s=%.1f rsi=%.1f htf=%s vol=%s",
                  symbol, blocked, sig.fast_ema, sig.slow_ema, sig.rsi,
                  sig.htf_trend, sig.vol_regime)
        return

    # ------------------------------------------------------------------
    # 7. Validate stop-loss / take-profit
    # ------------------------------------------------------------------
    if sig.stop_loss is None or sig.take_profit is None:
        log.warning("[%s] Signal has no SL/TP — cannot open trade.", symbol)
        return

    entry_price = mid_price

    sl_ok, sl_reason = risk.validate_sl_tp(
        sig.direction, entry_price, sig.stop_loss, sig.take_profit
    )
    if not sl_ok:
        log.warning("[%s] Invalid SL/TP: %s", symbol, sl_reason)
        return

    # ------------------------------------------------------------------
    # 8. Size the position
    # ------------------------------------------------------------------
    sz_decimals = market.get_sz_decimals(symbol)
    size = risk.compute_position_size(
        account_equity=equity,
        entry_price=entry_price,
        stop_loss_price=sig.stop_loss,
        sz_decimals=sz_decimals,
    )
    if size <= 0:
        log.warning("[%s] Position size computed as 0 — skipping.", symbol)
        return

    # ------------------------------------------------------------------
    # 9. Open the position
    # ------------------------------------------------------------------
    is_buy = sig.direction == Direction.LONG
    direction_str = "long" if is_buy else "short"

    log.info(
        "[%s] Opening %s: size=%.6f entry~=%.4f sl=%.4f tp=%.4f htf=%s vol=%s",
        symbol, direction_str, size, entry_price,
        sig.stop_loss, sig.take_profit, sig.htf_trend, sig.vol_regime,
    )

    result = exchange.place_market_order(
        coin=symbol,
        is_buy=is_buy,
        size=size,
        sz_decimals=sz_decimals,
    )

    if result is None:
        log.error("[%s] Order submission failed.", symbol)
        return

    # ------------------------------------------------------------------
    # 10. Record the position
    # ------------------------------------------------------------------
    record = PositionRecord(
        coin=symbol,
        direction=direction_str,
        entry_price=entry_price,
        size=size,
        stop_loss=sig.stop_loss,
        take_profit=sig.take_profit,
        signal=sig,
    )
    risk.record_open(record)

    log_position(
        symbol=symbol,
        side=direction_str,
        size=size,
        entry_price=entry_price,
        unrealised_pnl=0.0,
        stop_loss=sig.stop_loss,
        take_profit=sig.take_profit,
    )


def _manage_existing_positions(
    symbol: str,
    current_price: float,
    exchange: ExchangeClient,
    risk: RiskManager,
) -> None:
    """Check existing positions for SL/TP hits and close them."""
    record = risk.get_position_record(symbol)
    if record is None:
        return

    live_pos = exchange.get_position(symbol)
    if live_pos is None or float(live_pos.get("szi", 0)) == 0:
        log.info("[%s] Position no longer open on exchange — cleaning up.", symbol)
        risk.record_close(symbol)
        return

    unrealised_pnl = float(live_pos.get("unrealizedPnl", 0))
    log_position(
        symbol=symbol,
        side=record.direction,
        size=abs(float(live_pos.get("szi", 0))),
        entry_price=float(live_pos.get("entryPx", record.entry_price)),
        unrealised_pnl=unrealised_pnl,
        stop_loss=record.stop_loss,
        take_profit=record.take_profit,
    )

    should_close, reason = risk.should_close_position(record, current_price)
    if should_close:
        log.info("[%s] Closing %s position: %s", symbol, record.direction, reason)
        sz_decimals = 3
        exchange.close_position(symbol, sz_decimals=sz_decimals)
        risk.record_close(symbol)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_shutdown_requested = False

def _request_shutdown(exchange: ExchangeClient, risk: RiskManager, *args) -> None:
    global _shutdown_requested
    log.warning("Shutdown signal received — finishing current iteration.")
    _shutdown_requested = True

def _emergency_close_all(exchange: ExchangeClient, risk: RiskManager) -> None:
    if not risk.open_coins:
        return
    log.warning("Emergency close: shutting down %d position(s).", risk.open_count)
    for coin in list(risk.open_coins):
        try:
            exchange.close_position(coin)
            risk.record_close(coin)
        except Exception as exc:
            log.error("Failed to close %s during shutdown: %s", coin, exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=" * 60)
    log.info("Hyperliquid Trading Bot v2")
    log.info("Symbols: %s | Testnet: %s | Loop: %ds",
             ", ".join(config.SYMBOLS), config.USE_TESTNET, config.LOOP_INTERVAL)
    log.info("Strategy: EMA(%d/%d) + RSI(%d) + HTF(%s EMA%d) + VolFilter",
             config.STRATEGY.ema_fast, config.STRATEGY.ema_slow,
             config.STRATEGY.rsi_period,
             config.STRATEGY.htf_interval, config.STRATEGY.htf_ema_period)
    log.info("Risk: %.0f%%/trade | Max pos: %d | Drawdown halt: %.0f%%",
             config.RISK.max_risk_per_trade * 100,
             config.RISK.max_positions,
             config.RISK.max_drawdown_halt * 100)
    log.info("=" * 60)

    # --- Validate ---
    validate_config()

    # --- Optionally run backtest first ---
    if "--backtest" in sys.argv:
        log.info("Running backtest before going live...")
        from bot.backtest import run_backtest
        run_backtest()
        log.info("Backtest complete. Starting live trading...\n")

    # --- Initialise subsystems ---
    market   = MarketData()
    exchange = ExchangeClient()
    risk     = RiskManager()

    # --- Signal handlers ---
    shutdown_handler = partial(_request_shutdown, exchange, risk)
    signal.signal(signal.SIGINT,  shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # --- Set leverage for all symbols ---
    if config.RISK.leverage > 1:
        for sym in config.SYMBOLS:
            log.info("Setting leverage for %s to %d×", sym, config.RISK.leverage)
            exchange.set_leverage(sym, config.RISK.leverage, is_cross=True)

    # --- Main loop ---
    iteration = 0
    while not _shutdown_requested:
        iteration += 1
        loop_start = time.monotonic()
        log.info("--- Iteration %d ---", iteration)

        # Fetch equity once per iteration (shared across all symbols)
        equity = exchange.get_account_equity()
        if equity is None:
            log.warning("Could not fetch equity (API timeout?) — skipping iteration.")
            continue
        log.info("Equity: $%.2f | Positions: %d/%d | Halted: %s",
                 equity, risk.open_count, config.RISK.max_positions, risk.is_halted)
        risk.update_equity(equity)

        # Run strategy for each symbol
        for symbol in config.SYMBOLS:
            if _shutdown_requested:
                break
            run_iteration(symbol, market, exchange, risk, equity)

        elapsed = time.monotonic() - loop_start
        sleep_for = max(0.0, config.LOOP_INTERVAL - elapsed)
        log.debug("Iteration %d done in %.1fs. Sleeping %.1fs.", iteration, elapsed, sleep_for)

        sleep_end = time.monotonic() + sleep_for
        while time.monotonic() < sleep_end and not _shutdown_requested:
            time.sleep(min(1.0, sleep_end - time.monotonic()))

    # --- Shutdown ---
    log.info("Bot shutting down.")
    _emergency_close_all(exchange, risk)
    log.info("Bye.")


if __name__ == "__main__":
    main()
