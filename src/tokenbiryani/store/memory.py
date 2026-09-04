"""In-process state store. Zero dependencies; the default for a single instance."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from .base import StateStore


class MemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._affinity: Dict[str, Tuple[str, float]] = {}
        self._requests: Dict[str, Deque[float]] = defaultdict(deque)
        self._spend: Dict[str, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    async def get_affinity(self, session_key: str) -> Optional[str]:
        async with self._lock:
            entry = self._affinity.get(session_key)
            if entry is None:
                return None
            account_id, expires_at = entry
            if expires_at and expires_at < _now():
                del self._affinity[session_key]
                return None
            return account_id

    async def set_affinity(self, session_key: str, account_id: str, ttl: float) -> None:
        async with self._lock:
            self._affinity[session_key] = (account_id, _now() + ttl)

    async def clear_affinity(self, session_key: str) -> None:
        async with self._lock:
            self._affinity.pop(session_key, None)

    async def record_key_request(self, key_name: str, now: float) -> int:
        async with self._lock:
            bucket = self._requests[key_name]
            bucket.append(now)
            cutoff = now - 60.0
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            return len(bucket)

    async def add_spend(self, key_name: str, amount: float) -> float:
        async with self._lock:
            self._spend[key_name] += amount
            return self._spend[key_name]

    async def get_spend(self, key_name: str) -> float:
        async with self._lock:
            return self._spend[key_name]


def _now() -> float:
    import time

    return time.time()
