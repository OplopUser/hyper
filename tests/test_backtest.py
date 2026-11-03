"""Offline backtest tests on synthetic candles (no network, no keys)."""

import time
import unittest

import numpy as np
import pandas as pd

import config
from bot import backtest
from bot.backtest import Backtester, load_candles
from bot.strategy import Direction, Signal, compute_signal

# Filters off and RSI bounds open, so the only rule left is the EMA crossover
# and the scenario below is easy to reason about.
CFG = config.StrategyConfig(
    rsi_oversold=0.0, rsi_overbought=100.0,
    htf_enabled=False, vol_filter_enabled=False,
    candle_lookback=1000,
)


def _candles(closes, start="2026-01-01"):
    """15m candles whose open sits 0.5 above the previous close (so open != prior close)."""
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]] + 0.5
    return pd.DataFrame({
        "timestamp": pd.date_range(start, periods=len(closes), freq="15min", tz="UTC"),
        "open": opens,
        "high": np.maximum(opens, closes) + 3,
        "low": np.minimum(opens, closes) - 3,
        "close": closes,
        "volume": np.ones(len(closes)),
    })


def _scenario():
    """Down-trend, a sharp rally (bullish crossover on bar 76), then a crash."""
    down = 2000 - 2 * np.arange(70)
    up = down[-1] + 6 * np.arange(1, 9)
    crash = up[-1] - 15 * np.arange(1, 20)
    return _candles(np.r_[down, up, crash])


CROSS_BAR = 76   # last closed bar of the bullish crossover in _scenario()


class NoLookAheadTest(unittest.TestCase):
    def test_signal_ignores_the_forming_candle(self):
        """Changing the last (forming) row must not change the signal."""
        cfg = config.StrategyConfig(rsi_oversold=0.0, rsi_overbought=100.0, htf_enabled=False)
        df = _scenario().iloc[:CROSS_BAR + 2].reset_index(drop=True)
        base = compute_signal(df, cfg=cfg, dedupe=False)

        wild = df.copy()
        wild.loc[wild.index[-1], ["high", "low", "close"]] = [9_999.0, 1.0, 5_000.0]
        moved = compute_signal(wild, cfg=cfg, dedupe=False)

        self.assertEqual(base.direction, Direction.LONG)
        self.assertEqual(
            (moved.direction, moved.stop_loss, moved.vol_regime, moved.atr),
            (base.direction, base.stop_loss, base.vol_regime, base.atr),
        )

    def test_entry_on_next_open_after_closed_crossover(self):
        df = _scenario()
        trades = Backtester("TST", df, cfg=CFG, taker_fee=0.0).run().trades
        first = trades[0]
        self.assertEqual(first.direction, "long")
        self.assertEqual(first.entry_time, str(df.at[CROSS_BAR + 1, "timestamp"]))
        self.assertEqual(first.entry_price, df.at[CROSS_BAR + 1, "open"])
        self.assertNotEqual(first.entry_price, df.at[CROSS_BAR, "close"])

    def test_future_bars_do_not_change_past_trades(self):
        df = _scenario()
        full = Backtester("TST", df, cfg=CFG, taker_fee=0.0).run().trades
        cut = Backtester("TST", df.iloc[:CROSS_BAR + 2], cfg=CFG, taker_fee=0.0).run().trades
        self.assertEqual(cut[0].entry_time, full[0].entry_time)
        self.assertEqual(cut[0].entry_price, full[0].entry_price)

    def test_htf_window_only_has_closed_candles(self):
        df = _scenario()
        htf = df.iloc[::16].reset_index(drop=True)    # 4h candles from the same clock
        cfg = config.StrategyConfig(htf_enabled=True, htf_interval="4h")
        bt = Backtester("TST", df, htf_df=htf, cfg=cfg)
        i = 40                                         # 10:00 → 4h candles at 00:00 and 04:00 have closed
        window = bt._htf_window(i)
        bar_open = df.at[i, "timestamp"]
        closed = window.iloc[:-1]
        self.assertTrue((closed["timestamp"] + pd.Timedelta(hours=4) <= bar_open).all())
        self.assertEqual(len(closed), 2)
        self.assertEqual(window["close"].iloc[-1], df.at[i, "open"])   # forming 4h candle


class FeesAndRiskTest(unittest.TestCase):
    def test_taker_fee_charged_on_both_fills(self):
        fee = 0.001
        df = _scenario()
        free = Backtester("TST", df, cfg=CFG, taker_fee=0.0).run()
        paid = Backtester("TST", df, cfg=CFG, taker_fee=fee).run()

        t = paid.trades[0]
        expected = t.size * (t.entry_price + t.exit_price) * fee
        self.assertAlmostEqual(t.fees, expected, places=4)
        gross = (t.exit_price - t.entry_price) * t.size
        self.assertAlmostEqual(t.pnl, gross - expected, places=4)
        self.assertGreater(paid.total_fees, 0)
        self.assertLess(paid.total_pnl, free.total_pnl)
        self.assertAlmostEqual(paid.total_pnl, sum(x.pnl for x in paid.trades), places=3)

    def test_stop_loss_costs_risk_fraction_with_leverage(self):
        risk_cfg = config.RiskConfig(max_risk_per_trade=0.02, leverage=10)
        result = Backtester("TST", _scenario(), cfg=CFG, risk_cfg=risk_cfg,
                            taker_fee=0.0, initial_equity=1_000.0).run()
        stop = result.trades[0]
        self.assertEqual(stop.exit_reason, "stop_loss")
        self.assertAlmostEqual(stop.pnl, -20.0, places=2)    # 2 % of $1,000, not 10 × that


class ExitParityTest(unittest.TestCase):
    """Like live trading, only SL/TP close a position — not an opposite signal."""

    def test_opposite_signal_is_ignored_while_in_position(self):
        df = _candles(np.full(120, 2000.0))           # flat: SL/TP never touched
        long_at, short_at = 60, 65
        asked = []

        class _Scripted(Backtester):
            def _signal_at(self, i):
                asked.append(i)
                if i == long_at:
                    return Signal(direction=Direction.LONG, stop_loss=1_900.0, take_profit=2_200.0)
                if i == short_at:
                    return Signal(direction=Direction.SHORT, stop_loss=2_100.0, take_profit=1_800.0)
                return Signal(direction=Direction.NONE)

        trades = _Scripted("TST", df, cfg=CFG, taker_fee=0.0).run().trades
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].direction, "long")
        self.assertEqual(trades[0].exit_reason, "end_of_data")
        # No signal is evaluated while the position is open (live: can_open_trade blocks)
        self.assertNotIn(short_at, asked)
        self.assertEqual(max(asked), long_at)


class DaysWindowTest(unittest.TestCase):
    def test_trade_from_skips_earlier_bars(self):
        df = _scenario()
        trade_from = df.at[CROSS_BAR + 5, "timestamp"]
        result = Backtester("TST", df, cfg=CFG, trade_from=trade_from).run()
        self.assertEqual(result.total_candles, len(df) - (CROSS_BAR + 5))
        self.assertTrue(all(t.entry_time >= str(trade_from) for t in result.trades))

    def test_load_candles_honours_days_and_drops_forming_candle(self):
        now = pd.Timestamp(int(time.time() * 1000), unit="ms", tz="UTC")
        last_open = now.floor("15min")                 # still forming
        calls = {}

        class _Market:
            def get_candles_df(self, coin, interval, lookback_candles):
                calls[interval] = lookback_candles
                freq = "15min" if interval == "15m" else "4h"
                end = now.floor(freq)
                ts = pd.date_range(end=end, periods=lookback_candles, freq=freq, tz="UTC")
                n = len(ts)
                return pd.DataFrame({"timestamp": ts, "open": np.ones(n), "high": np.ones(n),
                                     "low": np.ones(n), "close": np.ones(n), "volume": np.ones(n)})

        df, htf_df, trade_from = load_candles(_Market(), "ETH", days=7, cfg=config.StrategyConfig())
        self.assertEqual(calls["15m"], 7 * 96 + 200)
        self.assertEqual(calls["4h"], 7 * 6 + 100)
        self.assertNotIn(last_open, set(df["timestamp"]))
        self.assertLessEqual(htf_df["timestamp"].iloc[-1] + pd.Timedelta(hours=4), now)
        self.assertAlmostEqual((now - trade_from).total_seconds(), 7 * 86_400, delta=5)

    def test_cli_parses_days_and_fee(self):
        args = backtest._parse_args(["ETH", "--days", "14", "--taker-fee", "0.0002"])
        self.assertEqual((args.symbols, args.days, args.taker_fee), (["ETH"], 14, 0.0002))


if __name__ == "__main__":
    unittest.main()
