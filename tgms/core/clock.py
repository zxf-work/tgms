"""Hybrid logical clock for transaction time (spec WP1.1).

tt = max(wall_clock_micros, last_tt + 1) — strictly monotonic, unique per
write batch. `last_tt` is persisted by the event log (each appended batch
records its tt); on open, the store re-seeds the clock from the log tail.
"""

from __future__ import annotations

import time
from typing import Callable


def wall_clock_micros() -> int:
    return time.time_ns() // 1_000


class HybridLogicalClock:
    def __init__(self, last_tt: int = 0, now_fn: Callable[[], int] = wall_clock_micros) -> None:
        self._last_tt = last_tt
        self._now_fn = now_fn

    @property
    def last_tt(self) -> int:
        return self._last_tt

    def tick(self) -> int:
        """Return the next transaction timestamp; strictly greater than any prior."""
        tt = max(self._now_fn(), self._last_tt + 1)
        self._last_tt = tt
        return tt

    def observe(self, tt: int) -> None:
        """Advance the clock past an externally observed tt (event-log replay)."""
        if tt > self._last_tt:
            self._last_tt = tt
