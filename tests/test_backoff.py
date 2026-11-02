"""Offline tests for the main-loop back-off (no network, no keys)."""

import unittest
from unittest import mock

from utils.backoff import Backoff


class BackoffTest(unittest.TestCase):
    def test_doubles_and_caps(self):
        b = Backoff(initial=5, maximum=60)
        self.assertEqual([b.failure() for _ in range(6)], [5, 10, 20, 40, 60, 60])

    def test_reset(self):
        b = Backoff(initial=5, maximum=60)
        b.failure(); b.failure()
        b.reset()
        self.assertEqual(b.failure(), 5)

    def test_long_outage_does_not_overflow(self):
        b = Backoff(initial=5, maximum=300)
        for _ in range(5000):
            delay = b.failure()
        self.assertEqual(delay, 300)

    def test_rejects_bad_args(self):
        with self.assertRaises(ValueError):
            Backoff(initial=0, maximum=10)


class _StubExchange:
    """Equity fails twice, then succeeds once."""

    def __init__(self, equities):
        self._equities = list(equities)

    def set_leverage(self, *a, **k):
        return True

    def get_account_equity(self):
        return self._equities.pop(0)


class MainLoopBackoffTest(unittest.TestCase):
    def test_equity_failure_sleeps_with_backoff_and_resets(self):
        import main

        sleeps = []
        calls = {"n": 0}

        def fake_sleep(seconds):
            sleeps.append(seconds)
            calls["n"] += 1
            if calls["n"] >= 4:
                main._shutdown_requested = True

        exch = _StubExchange([None, None, 1000.0, None])
        risk = mock.MagicMock(open_count=0, is_halted=False, open_coins=[])
        with mock.patch.object(main, "validate_config"), \
             mock.patch.object(main, "MarketData"), \
             mock.patch.object(main, "ExchangeClient", return_value=exch), \
             mock.patch.object(main, "RiskManager", return_value=risk), \
             mock.patch.object(main, "run_iteration"), \
             mock.patch.object(main.signal, "signal"), \
             mock.patch.object(main, "_interruptible_sleep", side_effect=fake_sleep), \
             mock.patch.object(main.config, "SYMBOLS", ["ETH"]), \
             mock.patch.object(main.config, "LOOP_INTERVAL", 60), \
             mock.patch.object(main.config, "ERROR_BACKOFF_INITIAL", 5.0), \
             mock.patch.object(main.config, "ERROR_BACKOFF_MAX", 300.0):
            main._shutdown_requested = False
            try:
                main.main()
            finally:
                main._shutdown_requested = False

        # fail -> 5s, fail -> 10s, success -> ~LOOP_INTERVAL, fail -> back to 5s
        self.assertEqual(sleeps[0], 5.0)
        self.assertEqual(sleeps[1], 10.0)
        self.assertGreater(sleeps[2], 50)
        self.assertEqual(sleeps[3], 5.0)


if __name__ == "__main__":
    unittest.main()
