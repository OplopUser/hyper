"""
utils/backoff.py — Capped exponential back-off for the main loop.

Usage:
    backoff = Backoff(initial=5, maximum=300)
    delay = backoff.failure()   # 5, 10, 20, ... capped at 300
    backoff.reset()             # after a success
"""

from __future__ import annotations


class Backoff:
    """Tracks consecutive failures and returns the delay to wait before retrying."""

    def __init__(self, initial: float, maximum: float, factor: float = 2.0) -> None:
        if initial <= 0 or maximum < initial or factor < 1:
            raise ValueError("Backoff needs 0 < initial <= maximum and factor >= 1")
        self.initial = initial
        self.maximum = maximum
        self.factor = factor
        self.failures = 0

    def failure(self) -> float:
        """Record one failure and return how long to sleep before retrying."""
        delay = self.initial
        for _ in range(self.failures):
            delay *= self.factor
            if delay >= self.maximum:
                break
        self.failures += 1
        return min(delay, self.maximum)

    def reset(self) -> None:
        """Call after a success so the next failure starts from *initial* again."""
        self.failures = 0
