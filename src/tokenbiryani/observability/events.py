"""Request events: the one stream the console, the CLI and the logs all read from."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set

logger = logging.getLogger("tokenbiryani")


@dataclass
class Attempt:
    account_id: str
    status: Optional[int] = None
    kind: str = ""
    latency: float = 0.0
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RequestEvent:
    request_id: str
    started_at: float
    model: str = ""
    key_name: str = ""
    session_key: str = ""
    streamed: bool = False
    priority: str = "interactive"
    #: "messages" (the normal path) or "batch" (spilled to the Batches API)
    via: str = "messages"
    batch_id: Optional[str] = None
    attempts: List[Attempt] = field(default_factory=list)
    account_id: Optional[str] = None
    status: Optional[int] = None
    latency: float = 0.0
    ttft: Optional[float] = None
    queued_for: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: Optional[float] = None
    saved_usd: Optional[float] = None
    affinity_broken: bool = False
    affinity_honored: bool = False
    decision: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    @property
    def cache_hit(self) -> bool:
        return self.cache_read_tokens > 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["attempts"] = [a.to_dict() for a in self.attempts]
        data["cache_hit"] = self.cache_hit
        return data


class EventLog:
    """A bounded ring buffer plus fan-out to live subscribers (the console's SSE feed)."""

    def __init__(self, capacity: int = 500) -> None:
        self._events: Deque[RequestEvent] = deque(maxlen=capacity)
        self._subscribers: Set[asyncio.Queue[Dict[str, Any]]] = set()

    def record(self, event: RequestEvent) -> None:
        self._events.append(event)
        payload = event.to_dict()
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A slow console must never apply backpressure to a request.
                pass
        logger.info("request %s", json.dumps(_log_view(payload), separators=(",", ":")))

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        events = list(self._events)[-limit:]
        return [e.to_dict() for e in reversed(events)]

    def recent_for(self, account_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Requests this account took part in, including attempts it failed."""
        matched = [
            event
            for event in self._events
            if event.account_id == account_id
            or any(attempt.account_id == account_id for attempt in event.attempts)
        ]
        return [e.to_dict() for e in reversed(matched[-limit:])]

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        for event in reversed(self._events):
            if event.request_id == request_id:
                return event.to_dict()
        return None

    def stats(self, window_seconds: float = 3600.0) -> Dict[str, Any]:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e.started_at >= cutoff]
        failovers = sum(1 for e in recent if len(e.attempts) > 1)
        breaks = sum(1 for e in recent if e.affinity_broken)
        errors = sum(1 for e in recent if e.status is None or e.status >= 400)
        billed = sum(
            e.input_tokens + e.cache_read_tokens + e.cache_creation_tokens for e in recent
        )
        cached = sum(e.cache_read_tokens for e in recent)
        spend = sum(e.cost_usd or 0.0 for e in recent)
        return {
            "window_seconds": window_seconds,
            "requests": len(recent),
            "failovers": failovers,
            "cache_breaks": breaks,
            "errors": errors,
            "cache_hit_rate": round(cached / billed, 4) if billed else None,
            "spend_usd": round(spend, 4),
        }

    def subscribe(self) -> asyncio.Queue[Dict[str, Any]]:
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Dict[str, Any]]) -> None:
        self._subscribers.discard(queue)


def _log_view(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Structured log line. Prompts are never in here — only accounting."""
    return {
        "request_id": payload["request_id"],
        "model": payload["model"],
        "key": payload["key_name"],
        "account": payload["account_id"],
        "status": payload["status"],
        "latency": round(payload["latency"], 3),
        "attempts": [
            {"account": a["account_id"], "status": a["status"], "kind": a["kind"]}
            for a in payload["attempts"]
        ],
        "tokens": {
            "in": payload["input_tokens"],
            "out": payload["output_tokens"],
            "cache_read": payload["cache_read_tokens"],
        },
        "cache_hit": payload["cache_hit"],
        "priority": payload["priority"],
        "via": payload["via"],
        "queued_for": payload["queued_for"],
        "affinity_broken": payload["affinity_broken"],
        "cost_usd": payload["cost_usd"],
    }
