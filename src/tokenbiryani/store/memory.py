"""In-process state store. Zero dependencies; loses everything on restart."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple

from .base import StateStore


class MemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._affinity: Dict[str, Tuple[str, float]] = {}
        self._requests: Dict[str, Deque[float]] = defaultdict(deque)
        self._ledger: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
        self._keys: Dict[str, Dict[str, object]] = {}
        self._accounts: Dict[str, Dict[str, object]] = {}
        self._lock = asyncio.Lock()

    async def get_affinity(self, session_key: str) -> Optional[str]:
        async with self._lock:
            entry = self._affinity.get(session_key)
            if entry is None:
                return None
            account_id, expires_at = entry
            if expires_at and expires_at < time.time():
                del self._affinity[session_key]
                return None
            return account_id

    async def set_affinity(self, session_key: str, account_id: str, ttl: float) -> None:
        async with self._lock:
            self._affinity[session_key] = (account_id, time.time() + ttl)

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

    async def add_spend(self, scope: str, name: str, amount: float) -> None:
        async with self._lock:
            self._ledger[f"{scope}:{name}"].append((time.time(), amount))

    async def get_spend(self, scope: str, name: str, window_seconds: float) -> float:
        async with self._lock:
            return self._sum(f"{scope}:{name}", window_seconds)

    async def spend_by_scope(self, scope: str, window_seconds: float) -> Dict[str, float]:
        prefix = scope + ":"
        async with self._lock:
            return {
                key[len(prefix):]: self._sum(key, window_seconds)
                for key in list(self._ledger)
                if key.startswith(prefix)
            }

    async def put_key(self, record: Dict[str, object]) -> None:
        async with self._lock:
            self._keys[str(record["name"])] = dict(record)

    async def delete_key(self, name: str) -> bool:
        async with self._lock:
            return self._keys.pop(name, None) is not None

    async def list_keys(self) -> List[Dict[str, object]]:
        async with self._lock:
            return [dict(record) for record in self._keys.values()]

    async def put_account(self, record: Dict[str, object]) -> None:
        async with self._lock:
            self._accounts[str(record["id"])] = dict(record)

    async def delete_account(self, account_id: str) -> bool:
        async with self._lock:
            return self._accounts.pop(account_id, None) is not None

    async def list_accounts(self) -> List[Dict[str, object]]:
        async with self._lock:
            return [dict(record) for record in self._accounts.values()]

    def _sum(self, key: str, window_seconds: float) -> float:
        cutoff = time.time() - window_seconds
        entries = self._ledger.get(key)
        if not entries:
            return 0.0
        # Prune while we are here; the ledger is append-only otherwise.
        kept = [entry for entry in entries if entry[0] >= cutoff]
        self._ledger[key] = kept
        return sum(amount for _, amount in kept)
