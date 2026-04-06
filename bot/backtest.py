"""
bot/backtest.py — Backtesting engine for the trading strategy.

Runs the strategy over historical candle data and produces performance
metrics without touching the exchange.

Usage:
    python -m bot.backtest             # backtest default symbols
    python -m bot.backtest ETH SOL     # backtest specific symbols

Features:
  - Uses the same compute_signal() and risk logic as the live bot
  - Simulates fills at candle close prices
  - Tracks equity curve, drawdown, win rate, Sharpe ratio
  - Outputs a per-trade log and summary to console
  - Saves results to backtest_results.json for the dashboard
"""

from __future__ import annotations

import json
import sys
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from bot.strategy import Direction, Signal, compute_signal, compute_htf_trend, compute_vol_regime
from data.market import MarketData
from utils.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    symbol: str
    direction: str           # "long" or "short"
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    exit_reason: str         # "stop_loss", "take_profit", "signal_reversal"
    stop_loss: float
    take_profit: float


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    period: str              # e.g. "2026-02-26 to 2026-03-28"
    total_candles: int
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    max_drawdown: float
    sharpe_ratio: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    filters_applied: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class Backtester:
    """
    Walk-forward backtester that simulates the strategy bar-by-bar.

    Uses the exact same compute_signal() function as the live bot,
    ensuring parity between backtest and live trading logic.
    """

    def __init__(
        self,
        symbol: str,
        df: pd.DataFrame,
        htf_df: Optional[pd.DataFrame] = None,
        initial_equity: float = 1000.0,
        cfg=None,
        risk_cfg=None,
    ):
        self.symbol = symbol
        self.df = df.copy().reset_index(drop=True)
        self.htf_df = htf_df
        self.initial_equity = initial_equity
        self.cfg = cfg or config.STRATEGY
        self.risk_cfg = risk_cfg or config.RISK

        # State
        self.equity = initial_equity
        self.peak_equity = initial_equity
        self.max_drawdown = 0.0
        self.position: Optional[dict] = None   # {direction, entry_price, size, sl, tp, entry_idx}
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = [initial_equity]

    def run(self) -> BacktestResult:
        """Execute the backtest and return results."""
        log.info("[BT:%s] Starting backtest — %d candles, $%.0f initial equity",
                 self.symbol, len(self.df), self.initial_equity)

        min_window = max(self.cfg.ema_slow, self.cfg.rsi_period, self.cfg.atr_period) + 10

        for i in range(min_window, len(self.df)):
            self._process_bar(i)

        # Close any remaining position at the last bar
        if self.position:
            self._close_position(len(self.df) - 1, "end_of_data")

        return self._compile_results()

    def _process_bar(self, i: int) -> None:
        """Process a single bar (candle)."""
        current_price = float(self.df.iloc[i]["close"])

        # --- Check existing position for SL/TP ---
        if self.position:
            high = float(self.df.iloc[i]["high"])
            low  = float(self.df.iloc[i]["low"])

            if self.position["direction"] == "long":
                if low <= self.position["sl"]:
                    self._close_position(i, "stop_loss", price_override=self.position["sl"])
                    return
                if high >= self.position["tp"]:
                    self._close_position(i, "take_profit", price_override=self.position["tp"])
                    return
            else:  # short
                if high >= self.position["sl"]:
                    self._close_position(i, "stop_loss", price_override=self.position["sl"])
                    return
                if low <= self.position["tp"]:
                    self._close_position(i, "take_profit", price_override=self.position["tp"])
                    return

        # --- Compute signal on the lookback window ---
        lookback_start = max(0, i - self.cfg.candle_lookback)
        window = self.df.iloc[lookback_start:i + 1].copy()

        signal = compute_signal(window, htf_df=self.htf_df, symbol=self.symbol, cfg=self.cfg)

        if not signal.is_actionable:
            self.equity_curve.append(self._mark_equity(current_price))
            return

        # --- If we have an opposing position, close it first ---
        if self.position:
            pos_dir = self.position["direction"]
            if (signal.direction == Direction.LONG and pos_dir == "short") or \
               (signal.direction == Direction.SHORT and pos_dir == "long"):
                self._close_position(i, "signal_reversal")
            else:
                # Same direction — skip (already in the trade)
                self.equity_curve.append(self._mark_equity(current_price))
                return

        # --- Size and open position ---
        if signal.stop_loss is None:
            return

        price_risk = abs(current_price - signal.stop_loss)
        if price_risk == 0:
            return

        risk_amount = self.equity * self.risk_cfg.max_risk_per_trade
        raw_size = risk_amount / price_risk
        notional = raw_size * current_price

        if notional < self.risk_cfg.min_order_usd:
            self.equity_curve.append(self._mark_equity(current_price))
            return

        direction_str = "long" if signal.direction == Direction.LONG else "short"
        self.position = {
            "direction": direction_str,
            "entry_price": current_price,
            "size": raw_size,
            "sl": signal.stop_loss,
            "tp": signal.take_profit,
            "entry_idx": i,
        }

        self.equity_curve.append(self._mark_equity(current_price))

    def _close_position(
        self, i: int, reason: str, price_override: Optional[float] = None
    ) -> None:
        """Close the current position and record the trade."""
        pos = self.position
        if not pos:
            return

        exit_price = price_override or float(self.df.iloc[i]["close"])

        if pos["direction"] == "long":
            pnl = (exit_price - pos["entry_price"]) * pos["size"]
        else:
            pnl = (pos["entry_price"] - exit_price) * pos["size"]

        self.equity += pnl
        pnl_pct = pnl / (pos["entry_price"] * pos["size"]) * 100 if pos["size"] > 0 else 0

        # Track drawdown
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        dd = (self.peak_equity - self.equity) / self.peak_equity if self.peak_equity > 0 else 0
        self.max_drawdown = max(self.max_drawdown, dd)

        entry_ts = str(self.df.iloc[pos["entry_idx"]].get("timestamp", pos["entry_idx"]))
        exit_ts  = str(self.df.iloc[i].get("timestamp", i))

        trade = BacktestTrade(
            symbol=self.symbol,
            direction=pos["direction"],
            entry_time=entry_ts,
            entry_price=pos["entry_price"],
            exit_time=exit_ts,
            exit_price=exit_price,
            size=pos["size"],
            pnl=round(pnl, 4),
            pnl_pct=round(pnl_pct, 2),
            exit_reason=reason,
            stop_loss=pos["sl"],
            take_profit=pos["tp"],
        )
        self.trades.append(trade)
        self.position = None

    def _mark_equity(self, current_price: float) -> float:
        """Calculate current equity including unrealised PnL."""
        eq = self.equity
        if self.position:
            pos = self.position
            if pos["direction"] == "long":
                eq += (current_price - pos["entry_price"]) * pos["size"]
            else:
                eq += (pos["entry_price"] - current_price) * pos["size"]
        return round(eq, 4)

    def _compile_results(self) -> BacktestResult:
        wins   = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]

        total_win  = sum(t.pnl for t in wins)
        total_loss = abs(sum(t.pnl for t in losses))

        # Sharpe ratio (annualised, assuming 15m candles)
        if len(self.equity_curve) > 1:
            returns = pd.Series(self.equity_curve).pct_change().dropna()
            if returns.std() > 0:
                # ~35,000 15-min periods per year
                sharpe = (returns.mean() / returns.std()) * np.sqrt(35040)
            else:
                sharpe = 0.0
        else:
            sharpe = 0.0

        first_ts = str(self.df.iloc[0].get("timestamp", "?"))
        last_ts  = str(self.df.iloc[-1].get("timestamp", "?"))

        filters = []
        if self.cfg.htf_enabled:
            filters.append(f"HTF trend ({self.cfg.htf_interval} EMA{self.cfg.htf_ema_period})")
        if self.cfg.vol_filter_enabled:
            filters.append(f"Vol filter (ATR p{self.cfg.vol_low_percentile:.0f}-p{self.cfg.vol_high_percentile:.0f})")

        return BacktestResult(
            symbol=self.symbol,
            timeframe=self.cfg.candle_interval,
            period=f"{first_ts[:10]} to {last_ts[:10]}",
            total_candles=len(self.df),
            total_trades=len(self.trades),
            wins=len(wins),
            losses=len(losses),
            win_rate=round(len(wins) / len(self.trades) * 100, 1) if self.trades else 0,
            total_pnl=round(self.equity - self.initial_equity, 4),
            max_drawdown=round(self.max_drawdown * 100, 2),
            sharpe_ratio=round(sharpe, 2),
            avg_win=round(total_win / len(wins), 4) if wins else 0,
            avg_loss=round(-total_loss / len(losses), 4) if losses else 0,
            profit_factor=round(total_win / total_loss, 2) if total_loss > 0 else 999.99,
            trades=self.trades,
            equity_curve=self.equity_curve[-200:],  # Keep last 200 points for charting
            filters_applied=filters,
        )


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------

def run_backtest(
    symbols: Optional[List[str]] = None,
    days: int = 30,
    save_results: bool = True,
) -> List[BacktestResult]:
    """
    Run backtests for one or more symbols using live candle data.
    """
    symbols = symbols or config.SYMBOLS
    market = MarketData()
    results = []

    for symbol in symbols:
        log.info("=" * 50)
        log.info("Backtesting %s", symbol)
        log.info("=" * 50)

        # Fetch primary timeframe candles
        df = market.get_candles_df(
            coin=symbol,
            interval=config.STRATEGY.candle_interval,
            lookback_candles=config.STRATEGY.candle_lookback,
        )
        if df.empty:
            log.warning("No candle data for %s — skipping.", symbol)
            continue

        # Fetch higher timeframe candles
        htf_df = None
        if config.STRATEGY.htf_enabled:
            htf_df = market.get_candles_df(
                coin=symbol,
                interval=config.STRATEGY.htf_interval,
                lookback_candles=config.STRATEGY.htf_lookback,
            )
            if htf_df.empty:
                log.warning("No HTF candle data for %s — running without HTF filter.", symbol)
                htf_df = None

        bt = Backtester(
            symbol=symbol,
            df=df,
            htf_df=htf_df,
            initial_equity=1000.0,
        )
        result = bt.run()
        results.append(result)

        # Print summary
        print(f"\n{'═' * 55}")
        print(f"  {symbol} — Backtest Results ({result.period})")
        print(f"{'═' * 55}")
        print(f"  Candles:        {result.total_candles}")
        print(f"  Trades:         {result.total_trades}")
        print(f"  Win / Loss:     {result.wins} / {result.losses}")
        print(f"  Win rate:       {result.win_rate}%")
        print(f"  Total PnL:      ${result.total_pnl:+.2f}")
        print(f"  Max drawdown:   {result.max_drawdown:.1f}%")
        print(f"  Sharpe ratio:   {result.sharpe_ratio:.2f}")
        print(f"  Avg win:        ${result.avg_win:+.4f}")
        print(f"  Avg loss:       ${result.avg_loss:.4f}")
        print(f"  Profit factor:  {result.profit_factor}")
        print(f"  Filters:        {', '.join(result.filters_applied) or 'none'}")

        if result.trades:
            print(f"\n  {'─' * 51}")
            print(f"  {'Time':<16} {'Dir':<6} {'Entry':>10} {'Exit':>10} {'PnL':>10} {'Reason'}")
            print(f"  {'─' * 51}")
            for t in result.trades[-15:]:  # last 15 trades
                print(f"  {t.entry_time[-16:]:<16} {t.direction:<6} "
                      f"${t.entry_price:>9.2f} ${t.exit_price:>9.2f} "
                      f"${t.pnl:>+9.4f} {t.exit_reason}")

        print()

    # Save results to JSON for the dashboard
    if save_results:
        out_path = Path(__file__).resolve().parent.parent / "backtest_results.json"
        serialisable = []
        for r in results:
            d = asdict(r)
            d["trades"] = [asdict(t) for t in r.trades]
            serialisable.append(d)
        with open(out_path, "w") as f:
            json.dump(serialisable, f, indent=2, default=str)
        log.info("Backtest results saved to %s", out_path)

    return results


if __name__ == "__main__":
    symbols = sys.argv[1:] if len(sys.argv) > 1 else None
    run_backtest(symbols=symbols)
