"""A scriptable stand-in for the Anthropic API.

Every routing behaviour in this gateway is a reaction to something an upstream did:
a 429 with a retry-after, a 529, a stream that dies mid-flight, a limit header
counting down. None of that is testable against the real API without spending money
and waiting on real reset windows, so it lives here instead.

Ships in the package on purpose — contributors adding a routing strategy need it.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Deque, Dict, List, Optional

import httpx


def _rfc3339(epoch: float) -> str:
    return (
        _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass
class Behavior:
    """One scripted upstream response."""

    kind: str = "ok"
    retry_after: Optional[float] = None
    status: Optional[int] = None
    message: str = ""
    text: str = "ok"
    deltas: int = 3
    delay: float = 0.0
    #: for stream_disconnect: how many chunks to emit before dying
    chunks_before_failure: int = 0
    input_tokens: int = 100
    output_tokens: int = 20
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


def ok(text: str = "ok", **kwargs: Any) -> Behavior:
    return Behavior(kind="ok", text=text, **kwargs)


def rate_limit(retry_after: float = 30.0) -> Behavior:
    return Behavior(kind="rate_limit", retry_after=retry_after)


def overloaded() -> Behavior:
    return Behavior(kind="overloaded")


def server_error(status: int = 500) -> Behavior:
    return Behavior(kind="server_error", status=status)


def invalid_request(message: str = "messages: field required") -> Behavior:
    return Behavior(kind="invalid_request", message=message)


def auth_error() -> Behavior:
    return Behavior(kind="auth_error")


def model_not_permitted() -> Behavior:
    return Behavior(kind="model_not_permitted")


def transport_error() -> Behavior:
    return Behavior(kind="transport_error")


def stream_disconnect(after_chunks: int = 0) -> Behavior:
    """Die mid-stream. after_chunks=0 dies before any content delta."""
    return Behavior(kind="stream_disconnect", chunks_before_failure=after_chunks)


def slow_stream(delay: float = 0.05, deltas: int = 3) -> Behavior:
    return Behavior(kind="ok", delay=delay, deltas=deltas)


@dataclass
class CacheOutcome:
    input_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    hit: bool


@dataclass
class MockAccount:
    api_key: str
    requests_limit: int = 1000
    input_limit: int = 100_000
    output_limit: int = 20_000
    window_seconds: float = 60.0

    requests_remaining: Optional[int] = None
    input_remaining: Optional[int] = None
    output_remaining: Optional[int] = None
    reset_at: Optional[float] = None

    script: Deque[Behavior] = field(default_factory=deque)
    received: List[Dict[str, Any]] = field(default_factory=list)
    #: Model the prompt cache. Off by default so scripted behaviours stay exact;
    #: the benchmark turns it on, because cache economics are what it measures.
    cache_aware: bool = False
    cache_ttl: float = 300.0
    cached_prefixes: Dict[str, float] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0

    #: How many polls a submitted batch reports as in_progress before it ends.
    batch_polls_before_ready: int = 1
    #: Set to fail batch submission, e.g. to prove the gateway falls back to queueing.
    batch_submit_status: int = 200
    #: set to suppress rate-limit headers, mimicking an upstream that reports nothing
    emit_limit_headers: bool = True

    def __post_init__(self) -> None:
        if self.requests_remaining is None:
            self.requests_remaining = self.requests_limit
        if self.input_remaining is None:
            self.input_remaining = self.input_limit
        if self.output_remaining is None:
            self.output_remaining = self.output_limit
        if self.reset_at is None:
            self.reset_at = time.time() + self.window_seconds

    def headers(self) -> Dict[str, str]:
        if not self.emit_limit_headers:
            return {}
        reset = _rfc3339(self.reset_at or time.time())
        return {
            "anthropic-ratelimit-requests-limit": str(self.requests_limit),
            "anthropic-ratelimit-requests-remaining": str(max(0, self.requests_remaining or 0)),
            "anthropic-ratelimit-requests-reset": reset,
            "anthropic-ratelimit-input-tokens-limit": str(self.input_limit),
            "anthropic-ratelimit-input-tokens-remaining": str(max(0, self.input_remaining or 0)),
            "anthropic-ratelimit-input-tokens-reset": reset,
            "anthropic-ratelimit-output-tokens-limit": str(self.output_limit),
            "anthropic-ratelimit-output-tokens-remaining": str(
                max(0, self.output_remaining or 0)
            ),
            "anthropic-ratelimit-output-tokens-reset": reset,
        }

    def consume(self, behavior: Behavior) -> None:
        self.requests_remaining = max(0, (self.requests_remaining or 0) - 1)
        self.input_remaining = max(0, (self.input_remaining or 0) - behavior.input_tokens)
        self.output_remaining = max(0, (self.output_remaining or 0) - behavior.output_tokens)

    def cache_lookup(self, body: Dict[str, Any], now: float) -> CacheOutcome:
        """Was this request's stable prefix already cached on this credential?

        Anthropic's cache is per-credential, which is the entire reason the router
        has an affinity term. The mock reproduces that scoping and nothing else.
        """
        # The cacheable prefix is a conversation's stable head, not just the system
        # prompt: system, tools, and the opening message. Two sessions sharing a
        # system prompt still cache separately once their histories diverge.
        head = {k: body.get(k) for k in ("system", "tools") if body.get(k) is not None}
        messages = body.get("messages")
        if isinstance(messages, list) and messages:
            head["opening"] = messages[0]
        prefix = json.dumps(head, sort_keys=True)
        prefix_tokens = int(len(prefix) / 3.5)
        if prefix_tokens <= 0:
            return CacheOutcome(0, 0, 0, False)

        key = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        expires = self.cached_prefixes.get(key)
        hit = expires is not None and expires > now
        self.cached_prefixes[key] = now + self.cache_ttl
        if hit:
            self.cache_hits += 1
            return CacheOutcome(0, prefix_tokens, 0, True)
        self.cache_misses += 1
        return CacheOutcome(0, 0, prefix_tokens, False)

    def exhaust(self) -> None:
        """Drain this account's budget without changing its reset time."""
        self.requests_remaining = 0
        self.input_remaining = 0
        self.output_remaining = 0


class MockAnthropic:
    """Serves the Messages API well enough to exercise every routing path."""

    def __init__(self, accounts: Optional[Dict[str, MockAccount]] = None) -> None:
        self.accounts: Dict[str, MockAccount] = accounts or {}
        self.calls: List[Dict[str, Any]] = []
        self.batches: Dict[str, Dict[str, Any]] = {}
        #: Delay every response. Without this an in-process mock answers before the
        #: next request starts, and nothing concurrent ever actually overlaps.
        self.latency: float = 0.0

    def add(self, name: str, api_key: str, **kwargs: Any) -> MockAccount:
        account = MockAccount(api_key=api_key, **kwargs)
        self.accounts[name] = account
        return account

    def by_key(self, api_key: str) -> Optional[MockAccount]:
        for account in self.accounts.values():
            if account.api_key == api_key:
                return account
        return None

    def script(self, name: str, *behaviors: Behavior) -> None:
        self.accounts[name].script.extend(behaviors)

    def transport(self) -> httpx.MockTransport:
        # Always async, and latency is read per request: a transport built before a
        # test sets `latency` must still honour it. (Sync httpx clients are not
        # supported against this mock, which nothing needs.)
        async def handle(request: httpx.Request) -> httpx.Response:
            if self.latency > 0:
                await asyncio.sleep(self.latency)
            return self._handle(request)

        return httpx.MockTransport(handle)

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport(), **kwargs)

    # ---- request handling ----------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Public entry point, so the mock can also be driven by a real ASGI server."""
        return self._handle(request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        api_key = request.headers.get("x-api-key", "")
        account = self.by_key(api_key)
        if account is not None and "/messages/batches" in request.url.path:
            return self._handle_batch(request, account)
        if account is None:
            return httpx.Response(
                401,
                json={
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "invalid x-api-key"},
                },
            )

        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
        except ValueError:
            body = {}
        account.received.append(body)
        self.calls.append({"key": api_key, "path": request.url.path, "body": body})

        behavior = account.script.popleft() if account.script else Behavior(kind="ok")

        if behavior.kind == "transport_error":
            raise httpx.ConnectError("mock upstream refused the connection", request=request)

        if behavior.kind == "rate_limit":
            account.requests_remaining = 0
            account.input_remaining = 0
            headers = dict(account.headers())
            if behavior.retry_after is not None:
                headers["retry-after"] = str(int(behavior.retry_after))
            return httpx.Response(
                429,
                headers=headers,
                json={
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": "rate limit exceeded"},
                },
            )

        if behavior.kind == "overloaded":
            return httpx.Response(
                529,
                headers=account.headers(),
                json={
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "overloaded"},
                },
            )

        if behavior.kind == "server_error":
            return httpx.Response(
                behavior.status or 500,
                headers=account.headers(),
                json={
                    "type": "error",
                    "error": {"type": "api_error", "message": "internal server error"},
                },
            )

        if behavior.kind == "invalid_request":
            return httpx.Response(
                400,
                headers=account.headers(),
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": behavior.message or "bad request",
                    },
                },
            )

        if behavior.kind == "auth_error":
            return httpx.Response(
                401,
                json={
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "invalid api key"},
                },
            )

        if behavior.kind == "model_not_permitted":
            return httpx.Response(
                403,
                headers=account.headers(),
                json={
                    "type": "error",
                    "error": {
                        "type": "permission_error",
                        "message": "your account does not have access to this model",
                    },
                },
            )

        if account.cache_aware and behavior.kind == "ok":
            outcome = account.cache_lookup(body, time.time())
            behavior = replace(
                behavior,
                cache_read_tokens=outcome.cache_read_tokens,
                cache_creation_tokens=outcome.cache_creation_tokens,
            )

        account.consume(behavior)

        if body.get("stream"):
            return httpx.Response(
                200,
                headers=dict(account.headers(), **{"content-type": "text/event-stream"}),
                content=self._stream(behavior, body),
            )

        if behavior.kind == "stream_disconnect":
            # Non-streaming equivalent of a dropped connection.
            raise httpx.ReadError("mock upstream dropped the connection", request=request)

        return httpx.Response(
            200,
            headers=account.headers(),
            json=self._message_body(behavior, body),
        )

    # ---- Message Batches -----------------------------------------------------

    def _handle_batch(self, request: httpx.Request, account: MockAccount) -> httpx.Response:
        path = request.url.path
        tail = path.split("/messages/batches", 1)[1].strip("/")

        if not tail:  # POST /v1/messages/batches
            if account.batch_submit_status != 200:
                return httpx.Response(
                    account.batch_submit_status,
                    json={
                        "type": "error",
                        "error": {"type": "api_error", "message": "batch submit refused"},
                    },
                )
            payload = json.loads(request.content.decode("utf-8") or "{}")
            batch_id = f"msgbatch_{len(self.batches) + 1:04d}"
            self.batches[batch_id] = {
                "account": account,
                "requests": payload.get("requests") or [],
                "polls": 0,
                "cancelled": False,
            }
            self.calls.append({"key": account.api_key, "path": path, "body": payload})
            return httpx.Response(
                200,
                json={
                    "id": batch_id,
                    "type": "message_batch",
                    "processing_status": "in_progress",
                },
            )

        parts = tail.split("/")
        batch_id = parts[0]
        state = self.batches.get(batch_id)
        if state is None:
            return httpx.Response(
                404,
                json={
                    "type": "error",
                    "error": {"type": "not_found_error", "message": "no such batch"},
                },
            )

        if len(parts) > 1 and parts[1] == "cancel":
            state["cancelled"] = True
            return httpx.Response(
                200, json={"id": batch_id, "processing_status": "canceling"}
            )

        if len(parts) > 1 and parts[1] == "results":
            lines = []
            for entry in state["requests"]:
                behavior = Behavior(kind="ok", text="batched")
                lines.append(
                    json.dumps(
                        {
                            "custom_id": entry.get("custom_id"),
                            "result": {
                                "type": "succeeded",
                                "message": self._message_body(
                                    behavior, entry.get("params") or {}
                                ),
                            },
                        }
                    )
                )
            return httpx.Response(
                200,
                headers={"content-type": "application/x-jsonl"},
                content="\n".join(lines).encode("utf-8"),
            )

        state["polls"] += 1
        if state["cancelled"]:
            status = "canceled"
        elif state["polls"] > state["account"].batch_polls_before_ready:
            status = "ended"
        else:
            status = "in_progress"
        return httpx.Response(
            200,
            json={
                "id": batch_id,
                "type": "message_batch",
                "processing_status": status,
                "results_url": f"/v1/messages/batches/{batch_id}/results",
            },
        )

    def _message_body(self, behavior: Behavior, request_body: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "model": request_body.get("model", "mock-model"),
            "content": [{"type": "text", "text": behavior.text}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": behavior.input_tokens,
                "output_tokens": behavior.output_tokens,
                "cache_read_input_tokens": behavior.cache_read_tokens,
                "cache_creation_input_tokens": behavior.cache_creation_tokens,
            },
        }

    async def _stream(self, behavior: Behavior, request_body: Dict[str, Any]):
        def frame(event: str, data: Dict[str, Any]) -> bytes:
            return (f"event: {event}\ndata: {json.dumps(data)}\n\n").encode()

        emitted = 0

        yield frame(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "model": request_body.get("model", "mock-model"),
                    "content": [],
                    "usage": {
                        "input_tokens": behavior.input_tokens,
                        "output_tokens": 0,
                        "cache_read_input_tokens": behavior.cache_read_tokens,
                        "cache_creation_input_tokens": behavior.cache_creation_tokens,
                    },
                },
            },
        )
        emitted += 1
        if behavior.kind == "stream_disconnect" and emitted > behavior.chunks_before_failure:
            raise httpx.ReadError("mock upstream dropped the stream")

        yield frame(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        emitted += 1
        if behavior.kind == "stream_disconnect" and emitted > behavior.chunks_before_failure:
            raise httpx.ReadError("mock upstream dropped the stream")

        for index in range(behavior.deltas):
            if behavior.delay:
                await asyncio.sleep(behavior.delay)
            yield frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": behavior.text + str(index)},
                },
            )
            emitted += 1
            if behavior.kind == "stream_disconnect" and emitted > behavior.chunks_before_failure:
                raise httpx.ReadError("mock upstream dropped the stream")

        yield frame("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield frame(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": behavior.output_tokens},
            },
        )
        yield frame("message_stop", {"type": "message_stop"})
