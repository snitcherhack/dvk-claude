"""Clock abstraction: production uses persistent wall-clock time; tests use a fake."""

from __future__ import annotations

import time


class MonotonicClock:
    """Compatibility name for the original clock.

    Lease timestamps are stored in SQLite, so they must survive a controller
    restart.  Epoch milliseconds, unlike monotonic process time, can be safely
    compared by a new process.
    """
    def now(self) -> int:
        return time.time_ns() // 1_000_000


class FakeClock:
    def __init__(self, now: int = 0) -> None:
        self.value = now

    def now(self) -> int:
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds
