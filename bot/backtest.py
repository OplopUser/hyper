"""
bot/backtest.py — Backtesting engine for the trading strategy.

Runs the strategy over historical candle data and produces performance
metrics without touching the exchange (no keys needed).

Usage:
    python -m bot.backtest                        # SYMBOLS, last 30 days
    python -m bot.backtest ETH SOL --days 14      # specific symbols / window
    python -m bot.backtest --taker-fee 0.0004     # custom fee rate

Simulation rules:
  - Same compute_signal() as the live bot, fed only CLOSED candles: the
    decision for bar i uses bars <= i-1, and the entry fills at bar i's OPEN.
  - The 4h trend filter sees only 4h candles that had closed by bar i's
    open, plus a forming 4h candle priced at that open (what the live bot
    sees when it polls).
  - Same RiskManager sizing as live (loss at the stop = MAX_RISK_PER_TRADE
    × equity, capped by margin × leverage).
  - SL/TP are checked against each bar's high/low, including the entry bar.
    If both are touched in one bar, the stop is assumed to hit first. A bar
    that opens beyond a level fills at the open (gap).
  - Like the live bot, an open position is closed only by its SL/TP (or the
    end of the data): while it is open, no signal is evaluated, so an
    opposite crossover neither closes nor reverses it.
  - Every fill pays the taker fee on its notional.
  - Tracks equity curve, drawdown, win rate, Sharpe ratio; prints a per-trade
    log and summary, and saves backtest_results.json for the dashboard.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from bot.risk import RiskManager
from bot.strategy import Direction, Signal, compute_signal
from data.market import MarketData, _interval_to_ms
from utils.logger import get_logger

log = get_logger(__name__)

_DAY_MS = 86_400_000
_YEAR_MS = 365 * _DAY_MS
_EPOCH = pd.Timestamp(0, tz="UTC")


def _to_ms(ts):
    """Epoch milliseconds for a Timestamp or Series (any pandas resolution)."""
    delta = (ts - _EPOCH) // pd.Timedelta(milliseconds=1)
    return delta.to_numpy(dtype="int64") if isinstance(delta, pd.Series) else int(delta)


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
    pnl: float               # net of fees
    pnl_pct: float           # net PnL / entry notional
    fees: float
    exit_reason: str         # "stop_loss", "take_profit", "end_of_data"
    stop_loss: float
    take_profit: float


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    period: str              # e.g. "2026-02-26 to 2026-03-28"
    total_candles: int       # candles in the trading window (excludes warm-up)
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float         # net of fees
    total_fees: float
    max_drawdown: float
    sharpe_ratio: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    taker_fee: float = 0.0
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    filters_applied: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class Backtester:
    """
    Walk-forward backtester that simulates the strategy bar-by-bar.

    Uses the exact same compute_signal() function and RiskManager sizing as
    the live bot. *df* must contain closed candles only, oldest first, with a
    tz-aware "timestamp" column (candle open time). Bars before *trade_from*
    (and the indicator warm-up bars) are used as history only.
    """

    def __init__(
        self,
        symbol: str,
        df: pd.DataFrame,
        htf_df: Optional[pd.DataFrame] = None,
        initial_equity: float = 1000.0,
        cfg=None,
        risk_cfg=None,
        taker_fee: Optional[float] = None,
        trade_from: Optional[pd.Timestamp] = None,
        sz_decimals: int = 6,
    ):
        self.symbol = symbol
        self.df = df.copy().reset_index(drop=True)
        self.initial_equity = initial_equity
        self.cfg = cfg or config.STRATEGY
        self.risk_cfg = risk_cfg or config.RISK
        self.taker_fee = config.BACKTEST_TAKER_FEE if taker_fee is None else taker_fee
        self.trade_from = trade_from
        self.sz_decimals = sz_decimals
        self.risk = RiskManager(self.risk_cfg)
        self.interval_ms = _interval_to_ms(self.cfg.candle_interval)

        # Higher timeframe: precompute close times so each bar can select the
        # HTF candles that had closed by its open.
        self.htf_df: Optional[pd.DataFrame] = None
        if htf_df is not None and not htf_df.empty and self.cfg.htf_enabled:
            self.htf_df = htf_df.sort_values("timestamp").reset_index(drop=True)
            self._htf_ms = _interval_to_ms(self.cfg.htf_interval)
            self._htf_close_ms = _to_ms(self.htf_df["timestamp"]) + self._htf_ms

        # State
        self.equity = initial_equity
        self.position: Optional[dict] = None   # {direction, entry_price, size, sl, tp, entry_idx, entry_fee}
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = []
        self.start_idx = 0

    # ------------------------------------------------------------------

    def run(self) -> BacktestResult:
        """Execute the backtest and return results."""
        warmup = max(self.cfg.ema_slow, self.cfg.rsi_period, self.cfg.atr_period) + 10
        start = max(warmup, self.cfg.min_candles)
        if self.trade_from is not None:
            first = int(np.searchsorted(_to_ms(self.df["timestamp"]),
                                        _to_ms(pd.Timestamp(self.trade_from))))
            start = max(start, first)
        self.start_idx = min(start, len(self.df))

        log.info("[BT:%s] Starting backtest — %d candles (+%d warm-up), $%.0f equity, fee %.4f%%",
                 self.symbol, len(self.df) - self.start_idx, self.start_idx,
                 self.initial_equity, self.taker_fee * 100)

        self.equity_curve.append(self.initial_equity)
        for i in range(self.start_idx, len(self.df)):
            self._process_bar(i)

        # Close any remaining position at the last close
        if self.position:
            last = len(self.df) - 1
            self._close_position(last, "end_of_data", float(self.df.at[last, "close"]))
            self.equity_curve[-1] = round(self.equity, 4)

        return self._compile_results()

    def _process_bar(self, i: int) -> None:
        """
        Simulate bar *i*: decide at its open from bars <= i-1, then let its
        high/low hit SL/TP, then mark to its close.
        """
        open_px = float(self.df.at[i, "open"])

        # --- 1. Entry: signal from closed candles only, filled at this open.
        # As in live trading (RiskManager.can_open_trade), a coin with an
        # open position is not evaluated; only SL/TP can close it.
        if self.position is None:
            signal = self._signal_at(i)
            if signal.is_actionable:
                direction = "long" if signal.direction == Direction.LONG else "short"
                self._open_position(i, signal, direction, open_px)

        # --- 2. Intrabar SL/TP (includes a position opened at this open) ---
        if self.position:
            self._check_exits(i)

        # --- 3. Mark to close ---
        self.equity_curve.append(self._mark_equity(float(self.df.at[i, "close"])))

    def _signal_at(self, i: int) -> Signal:
        """
        compute_signal() over the candle_lookback bars ending at i. Bar i
        plays the still-forming candle, which compute_signal never reads; its
        high/low/close are also overwritten with its open so nothing after
        the open can leak in.
        """
        lookback_start = max(0, i - self.cfg.candle_lookback + 1)
        window = self.df.iloc[lookback_start:i + 1].copy()
        window.loc[i, ["high", "low", "close"]] = window.at[i, "open"]
        if "volume" in window:
            window.loc[i, "volume"] = 0.0
        return compute_signal(window, htf_df=self._htf_window(i),
                              symbol=self.symbol, cfg=self.cfg, dedupe=False)

    def _htf_window(self, i: int) -> Optional[pd.DataFrame]:
        """HTF candles closed by bar i's open, plus the forming one priced at that open."""
        if self.htf_df is None:
            return None
        bar_open_ms = _to_ms(self.df.at[i, "timestamp"])
        n_closed = int(np.searchsorted(self._htf_close_ms, bar_open_ms, side="right"))
        cols = ["timestamp", "open", "high", "low", "close"]
        closed = self.htf_df.iloc[max(0, n_closed - self.cfg.htf_lookback + 1):n_closed][cols]
        if closed.empty:
            return None
        px = float(self.df.at[i, "open"])
        forming = pd.DataFrame([{
            "timestamp": closed["timestamp"].iloc[-1] + pd.Timedelta(milliseconds=self._htf_ms),
            "open": px, "high": px, "low": px, "close": px,
        }])
        return pd.concat([closed, forming], ignore_index=True)

    def _open_position(self, i: int, signal: Signal, direction: str, entry_price: float) -> None:
        if signal.stop_loss is None or signal.take_profit is None:
            return
        ok, reason = self.risk.validate_sl_tp(signal.direction, entry_price,
                                              signal.stop_loss, signal.take_profit)
        if not ok:
            log.debug("[BT:%s] bar %d: entry skipped, %s", self.symbol, i, reason)
            return
        size = self.risk.compute_position_size(
            account_equity=self.equity,
            entry_price=entry_price,
            stop_loss_price=signal.stop_loss,
            sz_decimals=self.sz_decimals,
        )
        if size <= 0:
            return
        entry_fee = size * entry_price * self.taker_fee
        self.equity -= entry_fee
        self.position = {
            "direction": direction,
            "entry_price": entry_price,
            "size": size,
            "sl": signal.stop_loss,
            "tp": signal.take_profit,
            "entry_idx": i,
            "entry_fee": entry_fee,
        }

    def _check_exits(self, i: int) -> None:
        o = float(self.df.at[i, "open"])
        h = float(self.df.at[i, "high"])
        l = float(self.df.at[i, "low"])
        pos = self.position
        sl, tp = pos["sl"], pos["tp"]
        if pos["direction"] == "long":
            if l <= sl:                                   # stop first (conservative)
                self._close_position(i, "stop_loss", min(o, sl))
            elif h >= tp:
                self._close_position(i, "take_profit", max(o, tp))
        else:
            if h >= sl:
                self._close_position(i, "stop_loss", max(o, sl))
            elif l <= tp:
                self._close_position(i, "take_profit", min(o, tp))

    def _close_position(self, i: int, reason: str, exit_price: float) -> None:
        """Close the current position at *exit_price* and record the trade."""
        pos = self.position
        if not pos:
            return

        if pos["direction"] == "long":
            gross = (exit_price - pos["entry_price"]) * pos["size"]
        else:
            gross = (pos["entry_price"] - exit_price) * pos["size"]
        exit_fee = pos["size"] * exit_price * self.taker_fee
        self.equity += gross - exit_fee

        fees = pos["entry_fee"] + exit_fee
        pnl = gross - fees
        entry_notional = pos["entry_price"] * pos["size"]
        pnl_pct = pnl / entry_notional * 100 if entry_notional > 0 else 0.0

        self.trades.append(BacktestTrade(
            symbol=self.symbol,
            direction=pos["direction"],
            entry_time=str(self.df.at[pos["entry_idx"], "timestamp"]),
            entry_price=pos["entry_price"],
            exit_time=str(self.df.at[i, "timestamp"]),
            exit_price=exit_price,
            size=pos["size"],
            pnl=round(pnl, 4),
            pnl_pct=round(pnl_pct, 2),
            fees=round(fees, 4),
            exit_reason=reason,
            stop_loss=pos["sl"],
            take_profit=pos["tp"],
        ))
        self.position = None

    def _mark_equity(self, price: float) -> float:
        """Equity including unrealised PnL (the exit fee is charged at the exit)."""
        eq = self.equity
        if self.position:
            pos = self.position
            if pos["direction"] == "long":
                eq += (price - pos["entry_price"]) * pos["size"]
            else:
                eq += (pos["entry_price"] - price) * pos["size"]
        return round(eq, 4)

    def _compile_results(self) -> BacktestResult:
        wins   = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]

        total_win  = sum(t.pnl for t in wins)
        total_loss = abs(sum(t.pnl for t in losses))

        # Max drawdown on the mark-to-market equity curve
        curve = pd.Series(self.equity_curve, dtype=float)
        max_dd = float(((curve.cummax() - curve) / curve.cummax()).max()) if len(curve) else 0.0

        # Sharpe ratio of per-bar returns, annualised for the candle interval
        sharpe = 0.0
        if len(curve) > 1:
            returns = curve.pct_change().dropna()
            if returns.std() > 0:
                sharpe = float(returns.mean() / returns.std() * np.sqrt(_YEAR_MS / self.interval_ms))

        window = self.df.iloc[self.start_idx:]
        first_ts = str(window["timestamp"].iloc[0]) if len(window) else "?"
        last_ts  = str(window["timestamp"].iloc[-1]) if len(window) else "?"

        filters = []
        if self.cfg.htf_enabled and self.htf_df is not None:
            filters.append(f"HTF trend ({self.cfg.htf_interval} EMA{self.cfg.htf_ema_period})")
        if self.cfg.vol_filter_enabled:
            filters.append(f"Vol filter (ATR p{self.cfg.vol_low_percentile:.0f}-p{self.cfg.vol_high_percentile:.0f})")

        return BacktestResult(
            symbol=self.symbol,
            timeframe=self.cfg.candle_interval,
            period=f"{first_ts[:10]} to {last_ts[:10]}",
            total_candles=len(window),
            total_trades=len(self.trades),
            wins=len(wins),
            losses=len(losses),
            win_rate=round(len(wins) / len(self.trades) * 100, 1) if self.trades else 0,
            total_pnl=round(self.equity - self.initial_equity, 4),
            total_fees=round(sum(t.fees for t in self.trades), 4),
            max_drawdown=round(max_dd * 100, 2),
            sharpe_ratio=round(sharpe, 2),
            avg_win=round(total_win / len(wins), 4) if wins else 0,
            avg_loss=round(-total_loss / len(losses), 4) if losses else 0,
            profit_factor=round(total_win / total_loss, 2) if total_loss > 0 else 999.99,
            taker_fee=self.taker_fee,
            trades=self.trades,
            equity_curve=self.equity_curve[-200:],  # Keep last 200 points for charting
            filters_applied=filters,
        )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _closed_only(df: pd.DataFrame, interval: str, now_ms: int) -> pd.DataFrame:
    """Drop candles that had not closed at *now_ms* (the forming one)."""
    if df.empty:
        return df
    close_ms = _to_ms(df["timestamp"]) + _interval_to_ms(interval)
    return df[close_ms <= now_ms].reset_index(drop=True)


def load_candles(market: MarketData, symbol: str, days: int, cfg=None):
    """
    Fetch closed candles covering the last *days* days plus indicator warm-up.

    Returns (df, htf_df, trade_from). Hyperliquid's candle endpoint serves
    at most the 5000 most recent candles per interval (about 52 days of 15m
    candles), so longer windows come back shorter; the printed period shows
    what was actually tested.
    """
    cfg = cfg or config.STRATEGY
    now_ms = int(time.time() * 1000)
    per_day = _DAY_MS // _interval_to_ms(cfg.candle_interval)
    df = market.get_candles_df(
        coin=symbol,
        interval=cfg.candle_interval,
        lookback_candles=days * per_day + cfg.candle_lookback,
    )
    df = _closed_only(df, cfg.candle_interval, now_ms)

    htf_df = None
    if cfg.htf_enabled:
        htf_per_day = max(1, _DAY_MS // _interval_to_ms(cfg.htf_interval))
        htf_df = market.get_candles_df(
            coin=symbol,
            interval=cfg.htf_interval,
            lookback_candles=days * htf_per_day + cfg.htf_lookback,
        )
        htf_df = _closed_only(htf_df, cfg.htf_interval, now_ms)
        if htf_df.empty:
            htf_df = None

    trade_from = pd.Timestamp(now_ms - days * _DAY_MS, unit="ms", tz="UTC")
    return df, htf_df, trade_from


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------

def run_backtest(
    symbols: Optional[List[str]] = None,
    days: int = 30,
    save_results: bool = True,
    taker_fee: Optional[float] = None,
    initial_equity: float = 1000.0,
) -> List[BacktestResult]:
    """
    Run backtests for one or more symbols over the last *days* days of
    Hyperliquid candles (from the network selected by USE_TESTNET).
    """
    symbols = symbols or config.SYMBOLS
    market = MarketData()
    results = []

    for symbol in symbols:
        log.info("=" * 50)
        log.info("Backtesting %s over the last %d day(s)", symbol, days)
        log.info("=" * 50)

        df, htf_df, trade_from = load_candles(market, symbol, days)
        if df.empty:
            log.warning("No candle data for %s — skipping.", symbol)
            continue
        if config.STRATEGY.htf_enabled and htf_df is None:
            log.warning("No HTF candle data for %s — running without HTF filter.", symbol)

        bt = Backtester(
            symbol=symbol,
            df=df,
            htf_df=htf_df,
            initial_equity=initial_equity,
            taker_fee=taker_fee,
            trade_from=trade_from,
            sz_decimals=market.get_sz_decimals(symbol),
        )
        result = bt.run()
        results.append(result)
        _print_result(result)

    # Save results to JSON for the dashboard
    if save_results:
        out_path = Path(__file__).resolve().parent.parent / "backtest_results.json"
        with open(out_path, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2, default=str)
        log.info("Backtest results saved to %s", out_path)

    return results


def _print_result(result: BacktestResult) -> None:
    print(f"\n{'═' * 55}")
    print(f"  {result.symbol} — Backtest Results ({result.period})")
    print(f"{'═' * 55}")
    print(f"  Candles:        {result.total_candles} × {result.timeframe}")
    print(f"  Trades:         {result.total_trades}")
    print(f"  Win / Loss:     {result.wins} / {result.losses}")
    print(f"  Win rate:       {result.win_rate}%")
    print(f"  Total PnL:      ${result.total_pnl:+.2f} (net of fees)")
    print(f"  Fees paid:      ${result.total_fees:.2f} ({result.taker_fee * 100:.3f}% taker)")
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
            print(f"  {t.entry_time[:16]:<16} {t.direction:<6} "
                  f"${t.entry_price:>9.2f} ${t.exit_price:>9.2f} "
                  f"${t.pnl:>+9.4f} {t.exit_reason}")
    print()


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest the strategy on Hyperliquid candles.")
    parser.add_argument("symbols", nargs="*", help="coins to test (default: SYMBOLS)")
    parser.add_argument("--days", type=int, default=30,
                        help="length of the trading window in days (default: 30)")
    parser.add_argument("--taker-fee", type=float, default=None,
                        help=f"fee rate per fill (default: {config.BACKTEST_TAKER_FEE})")
    parser.add_argument("--equity", type=float, default=1000.0,
                        help="starting equity in USD (default: 1000)")
    parser.add_argument("--no-save", action="store_true",
                        help="don't write backtest_results.json")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")
    if args.taker_fee is not None and args.taker_fee < 0:
        parser.error("--taker-fee must be >= 0")
    return args


if __name__ == "__main__":
    args = _parse_args()
    run_backtest(
        symbols=args.symbols or None,
        days=args.days,
        save_results=not args.no_save,
        taker_fee=args.taker_fee,
        initial_equity=args.equity,
    )
