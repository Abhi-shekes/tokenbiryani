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
from typing import Dict, List, Optional

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
    async def clear_affinity_for_account(self, account_id: str) -> int:
        """Drop every session pinned to this account; return how many. 

        Called when an account is deleted. Without it those sessions keep naming a
        credential that no longer exists for the rest of the affinity TTL: the
        router cannot match the owner, so it scores every candidate at zero
        affinity and reports a cache break on each one — noise attributed to
        routing for something routing did not do.

        Not called on *disable*, which is usually temporary. A disabled account
        that comes back should find its conversations still pinned to it.
        """

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

    @abc.abstractmethod
    async def record_usage(self, sample: Dict[str, object]) -> None:
        """Append one request's accounting to the usage history.

        Telemetry, not billing — `add_spend` remains the record a cap is enforced
        against. Losing a row here costs a notch on a chart, nothing more.
        """

    @abc.abstractmethod
    async def usage_rows(
        self, since: float, until: float, account_id: Optional[str] = None
    ) -> List[Dict[str, object]]:
        """Raw usage rows in a window. Bucketing happens in `observability.usage`."""

    @abc.abstractmethod
    async def put_key(self, record: Dict[str, object]) -> None:
        """Store a managed key record. Records hold a hash, never the key itself."""

    @abc.abstractmethod
    async def delete_key(self, name: str) -> bool:
        ...

    @abc.abstractmethod
    async def list_keys(self) -> List[Dict[str, object]]:
        ...

    @abc.abstractmethod
    async def put_account(self, record: Dict[str, object]) -> None:
        """Store an account added through the API. Credentials arrive encrypted."""

    @abc.abstractmethod
    async def delete_account(self, account_id: str) -> bool:
        ...

    @abc.abstractmethod
    async def list_accounts(self) -> List[Dict[str, object]]:
        ...

    async def put_setting(self, name: str, value: object) -> None:
        """Persist one operator setting made from the console.

        Not abstract: a store predating this method keeps working, it just forgets
        the setting at restart, which is a degradation and not a crash.
        """
        return None

    async def get_settings(self) -> Dict[str, object]:
        return {}

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
