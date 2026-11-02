"""Offline tests: positions left open across a restart are adopted, not doubled."""

import unittest
from unittest import mock

import numpy as np
import pandas as pd

import main
from bot.risk import RiskManager


def _candles(n=200, price=2000.0):
    rng = np.random.default_rng(0)
    close = price + rng.normal(0, 5, n).cumsum()
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC"),
        "open": close, "high": close + 3, "low": close - 3, "close": close,
        "volume": np.ones(n),
    })


class _Market:
    def __init__(self, df):
        self.df = df

    def get_candles_df(self, **kwargs):
        return self.df

    def get_sz_decimals(self, coin):
        return 4


class _Exchange:
    def __init__(self, positions):
        self.positions = positions
        self.closed = []

    def get_positions_or_none(self):
        return self.positions

    def close_position(self, coin, sz_decimals=3):
        self.closed.append((coin, sz_decimals))


class AdoptionTest(unittest.TestCase):
    def setUp(self):
        self.risk = RiskManager()

    def test_adopts_untracked_long_and_blocks_new_entry(self):
        exch = _Exchange([{"coin": "ETH", "szi": "0.5", "entryPx": "2000"}])
        ok = main._manage_existing_positions("ETH", 2000.0, _Market(_candles()), exch, self.risk)
        self.assertTrue(ok)
        rec = self.risk.get_position_record("ETH")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.direction, "long")
        self.assertEqual(rec.size, 0.5)
        self.assertLess(rec.stop_loss, 2000.0)
        self.assertGreater(rec.take_profit, 2000.0)
        self.assertEqual(exch.closed, [])
        allowed, reason = self.risk.can_open_trade("ETH", 10_000.0)
        self.assertFalse(allowed)
        self.assertIn("already open", reason)

    def test_adopted_short_closed_when_sl_already_hit(self):
        exch = _Exchange([{"coin": "ETH", "szi": "-0.5", "entryPx": "2000"}])
        main._manage_existing_positions("ETH", 1_000_000.0, _Market(_candles()), exch, self.risk)
        self.assertEqual(exch.closed, [("ETH", 4)])
        self.assertIsNone(self.risk.get_position_record("ETH"))

    def test_api_failure_blocks_trading_and_keeps_record(self):
        exch = _Exchange([{"coin": "ETH", "szi": "0.5", "entryPx": "2000"}])
        main._manage_existing_positions("ETH", 2000.0, _Market(_candles()), exch, self.risk)
        exch.positions = None  # user_state call failed
        ok = main._manage_existing_positions("ETH", 2000.0, _Market(_candles()), exch, self.risk)
        self.assertFalse(ok)
        self.assertIsNotNone(self.risk.get_position_record("ETH"))

    def test_no_candles_blocks_trading_until_adopted(self):
        exch = _Exchange([{"coin": "ETH", "szi": "0.5", "entryPx": "2000"}])
        ok = main._manage_existing_positions("ETH", 2000.0, _Market(pd.DataFrame()), exch, self.risk)
        self.assertFalse(ok)
        self.assertIsNone(self.risk.get_position_record("ETH"))

    def test_iteration_does_not_open_when_position_exists(self):
        exch = _Exchange([{"coin": "ETH", "szi": "0.5", "entryPx": "2000"}])
        exch.place_market_order = mock.Mock()
        market = _Market(_candles())
        market.get_mid_price = lambda coin: 2000.0
        main._iteration("ETH", market, exch, self.risk, 10_000.0)
        exch.place_market_order.assert_not_called()

    def test_shutdown_does_not_close_by_default(self):
        self.assertFalse(main.config.CLOSE_ON_SHUTDOWN)


if __name__ == "__main__":
    unittest.main()
