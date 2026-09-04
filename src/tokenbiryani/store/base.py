"""State store interface.

Everything shared between requests goes through here — affinity, per-key spend, per-key
request counts. Keeping it behind one interface is what makes the multi-instance Redis
backend an implementation rather than a rewrite.
"""

from __future__ import annotations

import abc
from typing import Optional


class StateStore(abc.ABC):
    @abc.abstractmethod
    async def get_affinity(self, session_key: str) -> Optional[str]:
        """Which account currently owns this session's prompt cache, if any."""

    @abc.abstractmethod
    async def set_affinity(self, session_key: str, account_id: str, ttl: float) -> None:
        ...

    @abc.abstractmethod
    async def clear_affinity(self, session_key: str) -> None:
        ...

    @abc.abstractmethod
    async def record_key_request(self, key_name: str, now: float) -> int:
        """Record a request against a virtual key; return its count in the last 60s."""

    @abc.abstractmethod
    async def add_spend(self, key_name: str, amount: float) -> float:
        ...

    @abc.abstractmethod
    async def get_spend(self, key_name: str) -> float:
        ...

    async def close(self) -> None:
        return None
