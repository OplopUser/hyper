"""Offline tests: position sizing risks a fixed fraction of equity at the stop."""

import unittest

import config
from bot.risk import RiskManager


def _risk(leverage=1, risk=0.02, min_order=10.0):
    return RiskManager(config.RiskConfig(
        max_risk_per_trade=risk, max_positions=3, leverage=leverage, min_order_usd=min_order,
    ))


class PositionSizeTest(unittest.TestCase):
    EQUITY = 10_000.0
    ENTRY = 2_000.0

    def size(self, rm, stop, **kw):
        return rm.compute_position_size(self.EQUITY, self.ENTRY, stop, sz_decimals=6, **kw)

    def test_loss_at_stop_is_risk_fraction(self):
        # 5 % stop: $200 risk / $100 per ETH → 2 ETH ($4,000 notional), inside the margin cap
        size = self.size(_risk(), stop=1_900.0)
        self.assertAlmostEqual(size, 2.0)
        self.assertAlmostEqual(size * (self.ENTRY - 1_900.0), 0.02 * self.EQUITY)

    def test_leverage_does_not_change_size(self):
        sizes = {lev: self.size(_risk(leverage=lev), stop=1_900.0) for lev in (1, 3, 10)}
        self.assertEqual(len(set(sizes.values())), 1, sizes)

    def test_short_stop_above_entry(self):
        size = self.size(_risk(leverage=5), stop=2_100.0)
        self.assertAlmostEqual(size * 100.0, 0.02 * self.EQUITY)

    def test_margin_cap_binds_at_low_leverage_only(self):
        # 0.5 % stop: uncapped size = 200 / 10 = 20 ETH = $40,000 notional
        capped = self.size(_risk(leverage=1), stop=1_990.0)
        self.assertAlmostEqual(capped, self.EQUITY * 0.95 / self.ENTRY, places=6)
        self.assertLess(capped * 10.0, 0.02 * self.EQUITY)   # risks less than 2 %

        free = self.size(_risk(leverage=5), stop=1_990.0)
        self.assertAlmostEqual(free, 20.0)
        self.assertAlmostEqual(free * 10.0, 0.02 * self.EQUITY)

    def test_cap_uses_available_margin(self):
        # Other positions tie up margin: only $1,000 free at 10x → $9,500 max notional
        size = self.size(_risk(leverage=10), stop=1_990.0, available_margin=1_000.0)
        self.assertAlmostEqual(size, 1_000.0 * 10 * 0.95 / self.ENTRY, places=6)

    def test_no_free_margin_gives_zero(self):
        self.assertEqual(self.size(_risk(leverage=10), stop=1_900.0, available_margin=0.0), 0.0)

    def test_rounds_down_to_size_decimals(self):
        size = _risk().compute_position_size(self.EQUITY, self.ENTRY, 1_970.0, sz_decimals=2)
        self.assertEqual(size, 4.75)   # cap 4.75 ETH; raw 6.67 → capped, floored to 0.01

    def test_below_minimum_order_is_zero(self):
        self.assertEqual(_risk(min_order=10.0).compute_position_size(100.0, 2_000.0, 1_000.0), 0.0)

    def test_degenerate_inputs(self):
        rm = _risk()
        self.assertEqual(rm.compute_position_size(0.0, 2_000.0, 1_900.0), 0.0)
        self.assertEqual(rm.compute_position_size(1_000.0, 2_000.0, 2_000.0), 0.0)


class DrawdownTest(unittest.TestCase):
    def test_current_drawdown_tracks_last_equity(self):
        rm = _risk()
        rm.update_equity(1_000.0)
        rm.update_equity(950.0)
        self.assertAlmostEqual(rm.current_drawdown, 0.05)
        self.assertFalse(rm.is_halted)


if __name__ == "__main__":
    unittest.main()
