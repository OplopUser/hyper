"""
main.py — Main entry point and bot loop for the Hyperliquid trading bot.

Run with:
    python main.py              # live trading
    python main.py --backtest   # run backtest first, then trade

The bot will:
  1. Validate environment / config on startup.
  2. Initialise all subsystems (market data, exchange client, risk manager).
  3. Enter the main loop for each symbol in SYMBOLS (every LOOP_INTERVAL seconds):
       a. Refresh account equity and check drawdown (on failure, back off
          with capped exponential delay instead of retrying immediately).
       b. Fetch primary timeframe candles + higher timeframe (4h) candles.
       c. Compute EMA crossover signal with RSI, HTF trend, and volatility filters.
       d. Manage existing positions (check SL/TP). A position already open on
          the exchange that the bot isn't tracking (e.g. left open by a
          restart) is adopted first, so it is managed and not doubled.
       e. Open new positions if signal is actionable and risk checks pass.
  4. On SIGINT/SIGTERM: exit, leaving positions open for the next process
     to adopt — unless CLOSE_ON_SHUTDOWN=true, which closes them first.

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
from bot.strategy import Direction, Signal, adopted_sl_tp, compute_signal
from data.market import MarketData
from utils.backoff import Backoff
from utils.logger import get_logger, log_position, log_risk_event

log = get_logger(__name__)


def _order_ok(result: Optional[dict]) -> bool:
    """Return True only if the SDK response clearly indicates a successful order."""
    if not result:
        return False
    if result.get("status") != "ok":
        return False
    # Defensive: Hyperliquid sometimes returns status=ok with per-order errors
    # in response.data.statuses[i]["error"]. Treat any such error as failure.
    try:
        statuses = result["response"]["data"]["statuses"]
        for s in statuses:
            if isinstance(s, dict) and "error" in s:
                return False
    except (KeyError, TypeError):
        pass
    return True


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
    if not _manage_existing_positions(symbol, mid_price, market, exchange, risk):
        # Live position state unknown — never open a new trade blind.
        return

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

    if not _order_ok(result):
        log.error("[%s] Order submission failed (result=%s)", symbol, result)
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


def _adopt_position(
    symbol: str,
    live_pos: dict,
    market: MarketData,
    risk: RiskManager,
) -> Optional[PositionRecord]:
    """
    Start tracking a position that is open on the exchange but not in memory
    (typically left open across a restart). SL/TP are rebuilt from the
    exchange entry price and the current ATR. Returns None if that fails.
    """
    szi = float(live_pos.get("szi", 0))
    entry_price = float(live_pos.get("entryPx") or 0)
    direction = Direction.LONG if szi > 0 else Direction.SHORT
    df = market.get_candles_df(
        coin=symbol,
        interval=config.STRATEGY.candle_interval,
        lookback_candles=config.STRATEGY.candle_lookback,
    )
    sig = adopted_sl_tp(df, direction, entry_price) if entry_price > 0 else None
    if sig is None:
        log.warning("[%s] Existing %s position found but SL/TP could not be computed "
                    "(no candle data?) — will retry; no new trades for %s until then.",
                    symbol, direction.name.lower(), symbol)
        return None

    record = PositionRecord(
        coin=symbol,
        direction="long" if szi > 0 else "short",
        entry_price=entry_price,
        size=abs(szi),
        stop_loss=sig.stop_loss,
        take_profit=sig.take_profit,
        signal=sig,
    )
    log.warning("[%s] Adopting existing %s position from exchange: size=%.6f entry=%.4f "
                "sl=%.4f tp=%.4f (SL/TP rebuilt from current ATR=%.4f)",
                symbol, record.direction, record.size, entry_price,
                record.stop_loss, record.take_profit, sig.atr)
    risk.record_open(record)
    return record


def _manage_existing_positions(
    symbol: str,
    current_price: float,
    market: MarketData,
    exchange: ExchangeClient,
    risk: RiskManager,
) -> bool:
    """
    Reconcile the tracked position for *symbol* with the exchange, adopt an
    untracked one, and close it on SL/TP. Returns False if the live position
    state is unknown (API failure, adoption failed) so the caller must not
    open a new trade for this symbol this iteration.
    """
    positions = exchange.get_positions_or_none()
    if positions is None:
        log.warning("[%s] Could not fetch positions — skipping symbol this iteration.", symbol)
        return False
    live_pos = next((p for p in positions if p.get("coin") == symbol), None)
    live_szi = float(live_pos.get("szi", 0)) if live_pos else 0.0

    record = risk.get_position_record(symbol)
    if record is not None and live_szi == 0:
        log.info("[%s] Position no longer open on exchange — cleaning up.", symbol)
        risk.record_close(symbol)
        return True
    if record is not None and (live_szi > 0) != (record.direction == "long"):
        log.warning("[%s] Exchange position side differs from tracked %s — re-adopting.",
                    symbol, record.direction)
        risk.record_close(symbol)
        record = None
    if record is None:
        if live_szi == 0:
            return True
        record = _adopt_position(symbol, live_pos, market, risk)
        if record is None:
            return False

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
        exchange.close_position(symbol, sz_decimals=market.get_sz_decimals(symbol))
        risk.record_close(symbol)
    return True


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_shutdown_requested = False

def _request_shutdown(exchange: ExchangeClient, risk: RiskManager, *args) -> None:
    global _shutdown_requested
    log.warning("Shutdown signal received — finishing current iteration.")
    _shutdown_requested = True

def _emergency_close_all(market: MarketData, exchange: ExchangeClient, risk: RiskManager) -> None:
    if not risk.open_coins:
        return
    log.warning("Emergency close: shutting down %d position(s).", risk.open_count)
    for coin in list(risk.open_coins):
        try:
            exchange.close_position(coin, sz_decimals=market.get_sz_decimals(coin))
            risk.record_close(coin)
        except Exception as exc:
            log.error("Failed to close %s during shutdown: %s", coin, exc)


def _interruptible_sleep(seconds: float) -> None:
    """Sleep up to *seconds*, waking early if a shutdown is requested."""
    sleep_end = time.monotonic() + seconds
    while time.monotonic() < sleep_end and not _shutdown_requested:
        time.sleep(max(0.0, min(1.0, sleep_end - time.monotonic())))


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
    log.info("Risk: %.0f%%/trade | Max pos: %d | Drawdown halt: %.0f%% | Close on shutdown: %s",
             config.RISK.max_risk_per_trade * 100,
             config.RISK.max_positions,
             config.RISK.max_drawdown_halt * 100,
             config.CLOSE_ON_SHUTDOWN)
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
    # Always set explicitly so the account's cross-leverage matches config
    # (otherwise we inherit whatever the account was last set to).
    for sym in config.SYMBOLS:
        log.info("Setting leverage for %s to %d× cross", sym, config.RISK.leverage)
        exchange.set_leverage(sym, config.RISK.leverage, is_cross=True)

    # --- Main loop ---
    # Positions already open on the exchange (e.g. left by a restart) are
    # adopted per symbol in _manage_existing_positions on the first iteration.
    backoff = Backoff(config.ERROR_BACKOFF_INITIAL, config.ERROR_BACKOFF_MAX)
    iteration = 0
    while not _shutdown_requested:
        iteration += 1
        loop_start = time.monotonic()
        log.info("--- Iteration %d ---", iteration)

        # Fetch equity once per iteration (shared across all symbols)
        equity = exchange.get_account_equity()
        if equity is None:
            delay = backoff.failure()
            log.warning("Could not fetch equity (API timeout?) — skipping iteration; "
                        "failure #%d, retrying in %.0fs.", backoff.failures, delay)
            _interruptible_sleep(delay)
            continue
        if backoff.failures:
            log.info("Equity fetch recovered after %d failed attempt(s).", backoff.failures)
            backoff.reset()
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

        _interruptible_sleep(sleep_for)

    # --- Shutdown ---
    log.info("Bot shutting down.")
    if config.CLOSE_ON_SHUTDOWN:
        _emergency_close_all(market, exchange, risk)
    elif risk.open_count:
        log.warning("Leaving %d position(s) open (%s) — CLOSE_ON_SHUTDOWN is off; the next "
                    "start re-adopts them. Until then nothing enforces their SL/TP.",
                    risk.open_count, ", ".join(risk.open_coins))
    log.info("Bye.")


if __name__ == "__main__":
    main()
