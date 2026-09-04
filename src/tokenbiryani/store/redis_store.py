"""Redis state store: shared affinity, spend and rate counters across instances.

This is what makes more than one gateway process safe to run. Without it, two
instances each keep their own affinity map and each enforce half a spend cap.

Named `redis_store` rather than `redis` so nothing in this package can shadow the
`redis` distribution for a reader skimming imports.

Requires the optional dependency:  pip install "tokenbiryani[redis]"
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from .base import StateStore

#: Ledger entries are stored as "<amount>|<nonce>" so two identical charges in the
#: same millisecond remain distinct members of the sorted set.
_SEPARATOR = "|"


def _connect(url: str) -> Any:
    try:
        import redis.asyncio as redis_asyncio
    except ImportError as exc:  # pragma: no cover - exercised by the error path only
        raise RuntimeError(
            "the redis store needs the redis package: pip install 'tokenbiryani[redis]'"
        ) from exc
    return redis_asyncio.from_url(url, decode_responses=True)


class RedisStateStore(StateStore):
    def __init__(
        self,
        url: str = "redis://127.0.0.1:6379/0",
        namespace: str = "tokenbiryani",
        client: Optional[Any] = None,
    ) -> None:
        self.url = url
        self.namespace = namespace
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _connect(self.url)
        return self._client

    def _key(self, *parts: str) -> str:
        return ":".join((self.namespace,) + parts)

    async def startup(self) -> None:
        await self.client.ping()

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            closer = getattr(self._client, "aclose", None) or getattr(
                self._client, "close", None
            )
            if closer is not None:
                await closer()
            self._client = None

    # ---- affinity ------------------------------------------------------------

    async def get_affinity(self, session_key: str) -> Optional[str]:
        return await self.client.get(self._key("affinity", session_key))

    async def set_affinity(self, session_key: str, account_id: str, ttl: float) -> None:
        # px, not ex: a sub-second TTL must not round down to "no expiry".
        await self.client.set(
            self._key("affinity", session_key), account_id, px=max(1, int(ttl * 1000))
        )

    async def clear_affinity(self, session_key: str) -> None:
        await self.client.delete(self._key("affinity", session_key))

    # ---- per-key request rate ------------------------------------------------

    async def record_key_request(self, key_name: str, now: float) -> int:
        key = self._key("requests", key_name)
        await self.client.zadd(key, {uuid.uuid4().hex: now})
        # "(" makes the upper bound exclusive. Redis is inclusive by default, which
        # would evict one entry more than the memory and SQLite backends and quietly
        # under-count the rate. The window keeps entries with score >= cutoff.
        await self.client.zremrangebyscore(key, "-inf", f"({now - 60.0}")
        # Expire the whole set if the key goes quiet, so idle keys cost nothing.
        await self.client.expire(key, 120)
        return int(await self.client.zcard(key))

    # ---- spend ledger --------------------------------------------------------

    async def add_spend(self, scope: str, name: str, amount: float) -> None:
        key = self._key("spend", scope, name)
        member = f"{amount}{_SEPARATOR}{uuid.uuid4().hex}"
        await self.client.zadd(key, {member: time.time()})
        await self.client.sadd(self._key("spend-names", scope), name)

    async def get_spend(self, scope: str, name: str, window_seconds: float) -> float:
        key = self._key("spend", scope, name)
        cutoff = time.time() - window_seconds
        await self.client.zremrangebyscore(key, "-inf", f"({cutoff}")
        members = await self.client.zrangebyscore(key, cutoff, "+inf")
        return sum(_amount(member) for member in members)

    # ---- managed keys --------------------------------------------------------

    async def put_key(self, record: Dict[str, Any]) -> None:
        await self.client.hset(
            self._key("keys"), str(record["name"]), json.dumps(record)
        )

    async def delete_key(self, name: str) -> bool:
        return bool(await self.client.hdel(self._key("keys"), name))

    async def list_keys(self) -> List[Dict[str, Any]]:
        raw = await self.client.hgetall(self._key("keys"))
        records = []
        for value in (raw or {}).values():
            try:
                records.append(json.loads(value))
            except ValueError:
                continue
        return records

    async def spend_by_scope(self, scope: str, window_seconds: float) -> Dict[str, float]:
        names = await self.client.smembers(self._key("spend-names", scope))
        totals: Dict[str, float] = {}
        for name in names:
            total = await self.get_spend(scope, name, window_seconds)
            if total:
                totals[name] = total
        return totals


def _amount(member: str) -> float:
    head = member.split(_SEPARATOR, 1)[0]
    try:
        return float(head)
    except ValueError:
        return 0.0
