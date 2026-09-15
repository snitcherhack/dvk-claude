"""Clock abstraction: production uses monotonic time; tests provide a fake."""

from __future__ import annotations

import time


class MonotonicClock:
    def now(self) -> int:
        return time.monotonic_ns() // 1_000_000


class FakeClock:
    def __init__(self, now: int = 0) -> None:
        self.value = now

    def now(self) -> int:
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds
