"""State store interface.

Everything shared between requests goes through here — affinity, windowed spend,
per-key request counts. Keeping it behind one interface is what makes the SQLite and
Redis backends implementations rather than rewrites.

Spend is a *ledger*, not a running total: entries carry a timestamp and callers ask
for a window. A lifetime total on a persistent store would eventually wedge the
gateway shut and stay that way.
"""

from __future__ import annotations

import abc
from typing import Dict, Optional

#: Spend scopes. Keys are billed to whoever presented them; accounts to the credential.
SCOPE_KEY = "key"
SCOPE_ACCOUNT = "account"


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
    async def add_spend(self, scope: str, name: str, amount: float) -> None:
        """Append to the spend ledger."""

    @abc.abstractmethod
    async def get_spend(self, scope: str, name: str, window_seconds: float) -> float:
        """Total spend for one name over the trailing window."""

    @abc.abstractmethod
    async def spend_by_scope(self, scope: str, window_seconds: float) -> Dict[str, float]:
        """Every name in a scope, for hydrating in-memory counters at startup."""

    async def startup(self) -> None:
        return None

    async def close(self) -> None:
        return None


def build_store(config) -> StateStore:
    """Construct the store named by `store.backend`."""
    backend = (config.store.backend or "memory").lower()
    if backend == "memory":
        from .memory import MemoryStateStore

        return MemoryStateStore()
    if backend == "sqlite":
        from .sqlite import SqliteStateStore

        return SqliteStateStore(config.store.path)
    if backend == "redis":
        from .redis_store import RedisStateStore

        return RedisStateStore(config.store.url, config.store.namespace)
    raise ValueError(
        f"unknown store backend {backend!r}; known: memory, sqlite, redis"
    )
