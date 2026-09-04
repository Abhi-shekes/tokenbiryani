"""Per-account circuit breaker: closed -> open -> half-open -> closed."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    cooldown_seconds: float = 30.0

    consecutive_failures: int = 0
    opened_at: Optional[float] = None
    _probe_in_flight: bool = False

    def state(self, now: float) -> BreakerState:
        if self.opened_at is None:
            return BreakerState.CLOSED
        if now - self.opened_at >= self.cooldown_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allow(self, now: float) -> bool:
        """Half-open lets exactly one probe through at a time."""
        state = self.state(now)
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            return False
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self._probe_in_flight = False

    def record_failure(self, now: float) -> None:
        self.consecutive_failures += 1
        self._probe_in_flight = False
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_at = now
