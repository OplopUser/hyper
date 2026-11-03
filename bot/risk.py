"""
bot/risk.py — Position sizing, drawdown monitoring, and stop-loss logic.

Responsibilities
----------------
1. Position sizing: given account equity and trade risk %, compute the
   number of contracts / coins to buy.
2. Drawdown monitoring: track peak equity and halt trading if the drawdown
   exceeds the configured threshold.
3. Max concurrent positions guard: refuse new orders if the limit is reached.
4. Stop-loss / take-profit validation: confirm SL and TP prices are sane
   relative to entry before an order is sent.
5. Minimum order size enforcement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

import config
from bot.strategy import Direction, Signal
from utils.logger import get_logger, log_risk_event

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

@dataclass
class PositionRecord:
    """Lightweight in-memory record of an open position tracked by the bot."""
    coin: str
    direction: str          # "long" or "short"
    entry_price: float
    size: float             # in base asset units (e.g. 0.001 BTC)
    stop_loss: float
    take_profit: float
    signal: Signal          # the signal that opened this position


class RiskManager:
    """
    Stateful risk-management layer.

    Create one instance per bot session and pass it to the main loop.
    """

    def __init__(self, risk_cfg=None) -> None:
        self._cfg = risk_cfg or config.RISK
        self._peak_equity: float = 0.0
        self._last_equity: float = 0.0
        self._trading_halted: bool = False
        self._positions: List[PositionRecord] = []

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def compute_position_size(
        self,
        account_equity: float,
        entry_price: float,
        stop_loss_price: float,
        sz_decimals: int = 3,
        available_margin: Optional[float] = None,
    ) -> float:
        """
        Compute how many units to trade so that hitting the stop-loss
        costs `max_risk_per_trade` of current equity — no more, whatever
        the leverage.

        Formula:
          risk_amount  = equity × max_risk_per_trade
          price_risk   = |entry_price - stop_loss_price|
          raw_size     = risk_amount / price_risk
          max_size     = available_margin × leverage × 0.95 / entry_price
          size         = floor(min(raw_size, max_size), sz_decimals)

        Leverage does not scale the size: the loss at the stop is
        size × price_risk, which is fixed by the risk budget. Leverage only
        changes how much margin the position ties up (notional / leverage),
        so its one effect here is to raise the margin cap. With tight stops
        and low leverage the cap can bind, and the trade then risks *less*
        than max_risk_per_trade.

        Parameters
        ----------
        account_equity   : Total account value in USD (sets the risk budget).
        entry_price      : Estimated fill price (e.g. current mid-price).
        stop_loss_price  : Price at which the stop-loss is placed.
        sz_decimals      : Exchange's required size precision.
        available_margin : Margin free for a new position (e.g. the
                           exchange's ``withdrawable``). Defaults to
                           account_equity, i.e. nothing else is open.

        Returns
        -------
        Order size in base units, or 0.0 if the order would be too small.
        """
        if account_equity <= 0:
            log.warning("compute_position_size: account_equity=%f, returning 0", account_equity)
            return 0.0
        if entry_price <= 0:
            log.warning("compute_position_size: entry_price=%f, returning 0", entry_price)
            return 0.0

        risk_amount = account_equity * self._cfg.max_risk_per_trade
        price_risk  = abs(entry_price - stop_loss_price)

        if price_risk == 0:
            log.warning("compute_position_size: price_risk is 0 (entry == stop_loss)")
            return 0.0

        raw_size = risk_amount / price_risk

        # Cap by the margin actually free for this trade: at most
        # margin × leverage of notional. Leave a 5% buffer for fees and IOC
        # slippage.
        leverage = max(self._cfg.leverage or 1, 1)
        margin = account_equity if available_margin is None else max(available_margin, 0.0)
        max_notional = margin * leverage * 0.95
        max_size_by_margin = max_notional / entry_price
        if raw_size > max_size_by_margin:
            log.info(
                "Size capped by margin: %.6f → %.6f (margin=$%.2f lev=%dx)",
                raw_size, max_size_by_margin, margin, leverage,
            )
            raw_size = max_size_by_margin

        # Round down to exchange precision
        size = math.floor(raw_size * 10**sz_decimals) / 10**sz_decimals

        # Minimum order size check (in USD notional)
        notional = size * entry_price
        if notional < self._cfg.min_order_usd:
            log.warning(
                "Computed order notional $%.2f < min $%.2f — skipping",
                notional, self._cfg.min_order_usd,
            )
            return 0.0

        log.debug(
            "Position size: equity=$%.2f risk=$%.2f price_risk=%.4f → size=%.6f "
            "(notional=$%.2f, margin=$%.2f at %dx)",
            account_equity, risk_amount, price_risk, size, notional,
            notional / leverage, leverage,
        )
        return size

    # ------------------------------------------------------------------
    # Pre-trade checks
    # ------------------------------------------------------------------

    def can_open_trade(
        self,
        coin: str,
        account_equity: float,
    ) -> tuple[bool, str]:
        """
        Return (True, "") if opening a new trade is permitted, or
        (False, reason_string) if it should be blocked.

        Checks:
          - Trading halt flag (drawdown exceeded)
          - Max concurrent positions limit
          - No existing position for the same coin
          - Non-zero equity
        """
        if self._trading_halted:
            return False, "trading halted due to max drawdown"

        if account_equity <= 0:
            return False, "account equity is zero or negative"

        if len(self._positions) >= self._cfg.max_positions:
            return False, (
                f"max concurrent positions reached "
                f"({len(self._positions)}/{self._cfg.max_positions})"
            )

        if any(p.coin == coin for p in self._positions):
            return False, f"position already open for {coin}"

        return True, ""

    def validate_sl_tp(
        self,
        direction: Direction,
        entry: float,
        stop_loss: float,
        take_profit: float,
    ) -> tuple[bool, str]:
        """
        Sanity-check stop-loss and take-profit levels.

        Rules:
          LONG:  stop_loss < entry < take_profit
          SHORT: take_profit < entry < stop_loss
        """
        if direction == Direction.LONG:
            if stop_loss >= entry:
                return False, f"LONG stop_loss ({stop_loss:.4f}) >= entry ({entry:.4f})"
            if take_profit <= entry:
                return False, f"LONG take_profit ({take_profit:.4f}) <= entry ({entry:.4f})"
        elif direction == Direction.SHORT:
            if stop_loss <= entry:
                return False, f"SHORT stop_loss ({stop_loss:.4f}) <= entry ({entry:.4f})"
            if take_profit >= entry:
                return False, f"SHORT take_profit ({take_profit:.4f}) >= entry ({entry:.4f})"

        return True, ""

    # ------------------------------------------------------------------
    # Drawdown monitoring
    # ------------------------------------------------------------------

    def update_equity(self, current_equity: float) -> None:
        """
        Call once per loop iteration to update peak equity and check
        if the drawdown halt threshold has been breached.
        """
        self._last_equity = current_equity
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
            log.debug("New peak equity: $%.2f", self._peak_equity)

        if self._peak_equity > 0:
            drawdown = (self._peak_equity - current_equity) / self._peak_equity
            if drawdown >= self._cfg.max_drawdown_halt and not self._trading_halted:
                self._trading_halted = True
                log_risk_event(
                    "DRAWDOWN_HALT",
                    f"equity=${current_equity:.2f} peak=${self._peak_equity:.2f} "
                    f"drawdown={drawdown:.1%} >= threshold={self._cfg.max_drawdown_halt:.1%}",
                )

    def reset_halt(self) -> None:
        """Manually re-enable trading after a drawdown halt (operator override)."""
        if self._trading_halted:
            log.warning("Drawdown halt manually reset — trading re-enabled.")
            self._trading_halted = False

    @property
    def is_halted(self) -> bool:
        return self._trading_halted

    @property
    def current_drawdown(self) -> float:
        """Drawdown of the last equity passed to update_equity(), from the peak."""
        if self._peak_equity <= 0:
            return 0.0
        return (self._peak_equity - self._last_equity) / self._peak_equity

    # ------------------------------------------------------------------
    # In-memory position tracking
    # ------------------------------------------------------------------

    def record_open(self, record: PositionRecord) -> None:
        """Register a newly opened position."""
        self._positions.append(record)
        log.info(
            "Position recorded: %s %s size=%.6f entry=%.4f sl=%.4f tp=%.4f",
            record.direction.upper(), record.coin, record.size,
            record.entry_price, record.stop_loss, record.take_profit,
        )

    def record_close(self, coin: str) -> Optional[PositionRecord]:
        """Remove and return the position record for *coin*, or None."""
        for i, pos in enumerate(self._positions):
            if pos.coin == coin:
                removed = self._positions.pop(i)
                log.info("Position closed and de-registered: %s %s", removed.direction, coin)
                return removed
        log.warning("record_close: no tracked position found for %s", coin)
        return None

    def get_position_record(self, coin: str) -> Optional[PositionRecord]:
        for pos in self._positions:
            if pos.coin == coin:
                return pos
        return None

    @property
    def open_count(self) -> int:
        return len(self._positions)

    @property
    def open_coins(self) -> List[str]:
        return [p.coin for p in self._positions]

    # ------------------------------------------------------------------
    # Existing position management
    # ------------------------------------------------------------------

    def should_close_position(
        self,
        record: PositionRecord,
        current_price: float,
    ) -> tuple[bool, str]:
        """
        Check if an existing position has hit its stop-loss or take-profit.

        Returns (True, reason) if it should be closed, (False, "") otherwise.
        """
        if record.direction == "long":
            if current_price <= record.stop_loss:
                return True, f"stop-loss hit: price={current_price:.4f} <= sl={record.stop_loss:.4f}"
            if current_price >= record.take_profit:
                return True, f"take-profit hit: price={current_price:.4f} >= tp={record.take_profit:.4f}"
        elif record.direction == "short":
            if current_price >= record.stop_loss:
                return True, f"stop-loss hit: price={current_price:.4f} >= sl={record.stop_loss:.4f}"
            if current_price <= record.take_profit:
                return True, f"take-profit hit: price={current_price:.4f} <= tp={record.take_profit:.4f}"

        return False, ""
