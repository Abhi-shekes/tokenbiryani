"""Admission control and the priority wait queue.

When nothing in the pool can serve a request, the honest options are to wait for a
known reset or to say so. Never to hang indefinitely: a caller told "four minutes"
can make a better decision than one left holding an open socket.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from dataclasses import dataclass, field
from typing import List, Optional

#: Lower number wins. Interactive traffic outranks batch.
PRIORITY_INTERACTIVE = 0
PRIORITY_BATCH = 10

PRIORITIES = {"interactive": PRIORITY_INTERACTIVE, "batch": PRIORITY_BATCH}

#: Clients name their priority; the queue orders by the number behind the name.
PRIORITY_HEADER = "x-tokenbiryani-priority"
MAX_WAIT_HEADER = "x-tokenbiryani-max-wait"


def parse_priority(value: Optional[str], default: int = PRIORITY_INTERACTIVE) -> int:
    if not value:
        return default
    return PRIORITIES.get(value.strip().lower(), default)


def priority_name(level: int) -> str:
    for name, value in PRIORITIES.items():
        if value == level:
            return name
    return str(level)


class QueueFull(Exception):
    """The wait queue is at capacity; shed load rather than grow latency silently."""


@dataclass(order=True)
class _Waiter:
    priority: int
    sequence: int
    event: asyncio.Event = field(compare=False, default_factory=asyncio.Event)


class CapacityGate:
    """A bounded, priority-ordered set of requests waiting for pool capacity.

    Waiters are woken on two signals only: a lease being released, and a rate-limit
    window resetting. Neither is a poll.
    """

    def __init__(self, max_size: int = 128, default_max_wait: float = 60.0) -> None:
        self.max_size = max_size
        self.default_max_wait = default_max_wait
        self._heap: List[_Waiter] = []
        self._counter = itertools.count()
        self._lock = asyncio.Lock()

    @property
    def depth(self) -> int:
        return len(self._heap)

    async def wait(self, priority: int, timeout: float) -> bool:
        """Join the queue. Returns True if woken, False if the deadline expired."""
        if len(self._heap) >= self.max_size:
            raise QueueFull(
                f"wait queue is full ({len(self._heap)} waiting)"
            )
        waiter = _Waiter(priority=priority, sequence=next(self._counter))
        async with self._lock:
            heapq.heappush(self._heap, waiter)
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=max(0.0, timeout))
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            async with self._lock:
                try:
                    self._heap.remove(waiter)
                    heapq.heapify(self._heap)
                except ValueError:
                    pass

    async def wake_one(self) -> None:
        """Capacity freed somewhere: wake the highest-priority waiter."""
        async with self._lock:
            if self._heap:
                self._heap[0].event.set()

    async def wake_all(self) -> None:
        """A reset window elapsed: let everyone re-evaluate."""
        async with self._lock:
            for waiter in self._heap:
                waiter.event.set()

    def snapshot(self) -> dict:
        return {
            "depth": len(self._heap),
            "max_size": self.max_size,
            "highest_priority": self._heap[0].priority if self._heap else None,
        }


@dataclass
class AdmissionResult:
    admitted: bool
    reason: Optional[str] = None
    retry_after: Optional[float] = None


def admission_check(
    any_account_could_serve: bool, soonest_available: Optional[float]
) -> AdmissionResult:
    """Fail fast when no reset could ever make this request servable."""
    if any_account_could_serve:
        return AdmissionResult(True)
    if soonest_available is None:
        return AdmissionResult(
            False,
            reason=(
                "no account in this pool can serve this request — it exceeds every "
                "account's rate limit, or none supports the requested model"
            ),
        )
    return AdmissionResult(False, reason="pool saturated", retry_after=soonest_available)
