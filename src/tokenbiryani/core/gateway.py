"""The gateway: admit, route, lease, proxy, reconcile, recover.

This is the seven-stage pipeline from the plan. The two subtleties worth reading for
are the lease lifecycle (reserve before dispatch, reconcile against real usage) and
the streaming commit boundary in ``stream`` — the single point where a failure stops
being hideable.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple

import httpx

from ..config import Config, KeyConfig
from ..observability.events import Attempt, EventLog, RequestEvent
from ..observability.metrics import Metrics
from ..providers.anthropic_api import build_upstream
from ..providers.base import BLOCKED_RESPONSE_HEADERS, Upstream
from ..proxy import sse
from ..proxy.errors import Classification, RequestAction, classify, classify_exception
from ..store.base import StateStore
from ..store.memory import MemoryStateStore
from .account import AccountRuntime, AccountState, Usage
from .breaker import CircuitBreaker
from .keys import KeyRegistry
from .limits import TokenEstimate, estimate_request
from .queue import PRIORITY_INTERACTIVE, CapacityGate, QueueFull, admission_check
from .router import Decision, Router, RoutingContext
from .session import session_key as compute_session_key

MESSAGES_PATH = "/v1/messages"


class GatewayError(Exception):
    """A failure the gateway itself is reporting, in Anthropic's error shape."""

    def __init__(
        self,
        status: int,
        message: str,
        kind: str = "api_error",
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.kind = kind
        self.retry_after = retry_after

    def payload(self) -> Dict[str, Any]:
        return {"type": "error", "error": {"type": self.kind, "message": self.message}}

    def headers(self) -> Dict[str, str]:
        if self.retry_after is None:
            return {}
        return {"retry-after": str(int(max(1, round(self.retry_after))))}


@dataclass
class Completion:
    status: int
    headers: Dict[str, str]
    content: bytes
    event: RequestEvent


def _response_headers(upstream_headers: Mapping[str, str], account_id: str) -> Dict[str, str]:
    out = {
        k: v for k, v in upstream_headers.items() if k.lower() not in BLOCKED_RESPONSE_HEADERS
    }
    out["x-tokenbiryani-account"] = account_id
    return out


class Gateway:
    def __init__(
        self,
        config: Config,
        store: Optional[StateStore] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.config = config
        self.store = store or MemoryStateStore()
        self.keys = KeyRegistry(config.keys)
        self.events = EventLog(config.observability.event_buffer)
        self.metrics = Metrics()
        self.gate = CapacityGate(config.queue.max_size, config.queue.default_max_wait_seconds)
        self.router = Router(config.routing.strategy, config.routing.weights)

        self.accounts: Dict[str, AccountRuntime] = {}
        self.upstreams: Dict[str, Upstream] = {}
        for account_config in config.accounts:
            self.accounts[account_config.id] = AccountRuntime(
                config=account_config,
                breaker=CircuitBreaker(
                    failure_threshold=config.breaker.failure_threshold,
                    cooldown_seconds=config.breaker.cooldown_seconds,
                ),
            )
            self.upstreams[account_config.id] = build_upstream(account_config)

        self._client = client
        self._owns_client = client is None
        self._register_metrics()

    # ---- lifecycle -----------------------------------------------------------

    async def startup(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.server.request_timeout_seconds, connect=10.0)
            )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("gateway.startup() has not run")
        return self._client

    # ---- public API ----------------------------------------------------------

    async def complete(
        self,
        body: Dict[str, Any],
        headers: Mapping[str, str],
        key: KeyConfig,
        path: str = MESSAGES_PATH,
    ) -> Completion:
        """Non-streaming Messages request, with transparent failover."""
        plan = await self._prepare(body, headers, key)
        last_error: Optional[GatewayError] = None

        while True:
            account, decision = await self._acquire(plan)
            result, classification, latency = await self._dispatch(account, plan, path)
            plan.event.attempts.append(
                Attempt(
                    account_id=account.id,
                    status=result.status if result else None,
                    kind=classification.kind,
                    latency=round(latency, 4),
                    detail=classification.detail[:200],
                )
            )
            plan.event.decision = decision.to_dict()

            if classification.kind == "ok" and result is not None:
                usage = Usage.from_body(result.body)
                await self._settle(account, plan, decision, usage, latency, result.status)
                return Completion(
                    status=result.status,
                    headers=_response_headers(result.headers, account.id),
                    content=result.raw,
                    event=plan.event,
                )

            account.apply(classification, time.time(), plan.model)
            self.metrics.incr(
                "tokenbiryani_upstream_failures_total",
                account=account.id,
                kind=classification.kind,
            )

            if classification.request_action is RequestAction.RETURN and result is not None:
                plan.event.status = result.status
                plan.event.error = classification.kind
                plan.event.latency = time.time() - plan.started
                self.events.record(plan.event)
                return Completion(
                    status=result.status,
                    headers=_response_headers(result.headers, account.id),
                    content=result.raw,
                    event=plan.event,
                )

            last_error = GatewayError(
                502 if result is None else result.status,
                classification.detail or classification.kind,
                kind=classification.kind,
            )
            if not self._may_retry(plan, account, classification):
                self._fail(plan, last_error)
                raise last_error
            await self._backoff(plan)

    async def stream(
        self,
        body: Dict[str, Any],
        headers: Mapping[str, str],
        key: KeyConfig,
        path: str = MESSAGES_PATH,
    ) -> Tuple[Dict[str, str], AsyncIterator[bytes]]:
        """Streaming Messages request.

        Failover is transparent right up until the first ``content_block_delta``
        reaches the client. After that the client is already rendering, so a failure
        can only be reported as an SSE error frame — that boundary is the one thing
        the gateway cannot hide, and it is documented rather than papered over.
        """
        plan = await self._prepare(body, headers, key, streamed=True)
        response_headers = {
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "connection": "keep-alive",
        }
        return response_headers, self._stream_iter(plan, path)

    # ---- internals -----------------------------------------------------------

    async def _prepare(
        self,
        body: Dict[str, Any],
        headers: Mapping[str, str],
        key: KeyConfig,
        streamed: bool = False,
    ) -> _Plan:
        model = str(body.get("model") or "")
        if not model:
            raise GatewayError(400, "request is missing 'model'", kind="invalid_request_error")
        if not key.supports_model(model):
            raise GatewayError(
                403,
                f"key {key.name!r} is not allowed to use model {model!r}",
                kind="permission_error",
            )

        now = time.time()
        if key.rpm is not None:
            count = await self.store.record_key_request(key.name, now)
            if count > key.rpm:
                raise GatewayError(
                    429,
                    f"key {key.name!r} is over its {key.rpm} rpm limit",
                    kind="rate_limit_error",
                    retry_after=60.0,
                )
        if key.spend_cap_usd is not None:
            spent = await self.store.get_spend(key.name)
            if spent >= key.spend_cap_usd:
                raise GatewayError(
                    429,
                    f"key {key.name!r} has reached its ${key.spend_cap_usd:.2f} spend cap",
                    kind="rate_limit_error",
                )

        session = compute_session_key(body, headers)
        owner = await self.store.get_affinity(session)
        estimate = estimate_request(body, self.config.routing.estimate_safety_margin)
        request_id = "req_" + uuid.uuid4().hex[:20]

        event = RequestEvent(
            request_id=request_id,
            started_at=now,
            model=model,
            key_name=key.name,
            session_key=session,
            streamed=streamed,
        )
        self.metrics.incr("tokenbiryani_requests_total", model=model, key=key.name)
        return _Plan(
            body=body,
            headers=dict(headers),
            key=key,
            model=model,
            estimate=estimate,
            session=session,
            owner=owner,
            event=event,
            started=now,
            deadline=now + self.config.retry.deadline_seconds,
        )

    async def _acquire(self, plan: _Plan) -> Tuple[AccountRuntime, Decision]:
        """Route, or wait on the queue for capacity that is known to be coming."""
        queue_started = time.time()
        while True:
            now = time.time()
            if now >= plan.deadline:
                waited = self.config.retry.deadline_seconds
                raise GatewayError(
                    504,
                    f"no capacity became available within {waited:.0f}s",
                    kind="timeout_error",
                )
            ctx = RoutingContext(
                model=plan.model,
                estimate=plan.estimate,
                session_key=plan.session,
                now=now,
                sticky_owner=plan.owner,
                pool=plan.key.pool,
                exclude=plan.excluded,
            )
            decision = self.router.select(self.accounts.values(), ctx)
            if decision.chosen is not None:
                plan.event.queued_for = round(time.time() - queue_started, 3)
                return decision.chosen, decision

            admission = admission_check(False, decision.soonest_available)
            if admission.retry_after is None:
                plan.event.decision = decision.to_dict()
                error = GatewayError(
                    503,
                    admission.reason or "no eligible account",
                    kind="overloaded_error",
                )
                self._fail(plan, error)
                raise error

            budget = min(
                plan.deadline - now,
                self.config.queue.default_max_wait_seconds,
                admission.retry_after + 0.25,
            )
            self.metrics.incr("tokenbiryani_queued_total", model=plan.model)
            try:
                await self.gate.wait(PRIORITY_INTERACTIVE, budget)
            except QueueFull as exc:
                error = GatewayError(
                    429,
                    str(exc),
                    kind="rate_limit_error",
                    retry_after=decision.soonest_available,
                )
                self._fail(plan, error)
                raise error from exc

    async def _dispatch(
        self, account: AccountRuntime, plan: _Plan, path: str
    ) -> Tuple[Any, Classification, float]:
        """One non-streaming attempt, with the lease held for its duration."""
        lease = account.mirror.reserve(plan.estimate, account.id)
        account.inflight += 1
        started = time.time()
        result = None
        try:
            result = await self.upstreams[account.id].send(
                self.client, path, plan.body, plan.headers
            )
            classification = classify(result.status, result.headers, result.body)
        except Exception as exc:  # noqa: BLE001 - transport failures are data here
            classification = classify_exception(exc)
        latency = time.time() - started
        now = time.time()
        if result is not None:
            account.mirror.update_from_headers(result.headers, now)
        usage = Usage.from_body(result.body if result else None)
        account.mirror.release(
            lease,
            actual_input=usage.billed_input or None,
            actual_output=usage.output_tokens or None,
            now=now,
        )
        account.inflight -= 1
        await self.gate.wake_one()
        return result, classification, latency

    async def _stream_iter(self, plan: _Plan, path: str) -> AsyncIterator[bytes]:
        while True:
            try:
                account, decision = await self._acquire(plan)
            except GatewayError as exc:
                yield sse.error_event(exc.message, exc.kind)
                return

            plan.event.decision = decision.to_dict()
            upstream = self.upstreams[account.id]
            lease = account.mirror.reserve(plan.estimate, account.id)
            account.inflight += 1
            started = time.time()
            collector = sse.UsageCollector()
            committed = False
            classification: Optional[Classification] = None
            status: Optional[int] = None

            try:
                context = upstream.open_stream(self.client, path, plan.body, plan.headers)
                response = await context.__aenter__()
                try:
                    status = response.status_code
                    if status != 200:
                        await response.aread()
                        body = _safe_json(response)
                        classification = classify(status, dict(response.headers), body)
                        account.mirror.update_from_headers(dict(response.headers), time.time())
                    else:
                        account.mirror.update_from_headers(dict(response.headers), time.time())
                        buffered: List[bytes] = []
                        grace = started + self.config.retry.first_token_grace_seconds
                        async for chunk in response.aiter_bytes():
                            collector.feed(chunk)
                            if not committed:
                                buffered.append(chunk)
                                seen_content = sse.FIRST_CONTENT_EVENT in b"".join(buffered)
                                if seen_content or time.time() >= grace:
                                    # Point of no return: flush and commit.
                                    committed = True
                                    if plan.event.ttft is None:
                                        plan.event.ttft = round(time.time() - started, 4)
                                    for pending in buffered:
                                        yield pending
                                    buffered = []
                            else:
                                yield chunk
                        if not committed:
                            # Stream ended before any delta — a complete, short response.
                            committed = True
                            if plan.event.ttft is None:
                                plan.event.ttft = round(time.time() - started, 4)
                            for pending in buffered:
                                yield pending
                        classification = classify(200)
                finally:
                    await context.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                classification = classify_exception(exc)

            collector.finish()
            latency = time.time() - started
            now = time.time()
            usage = Usage.from_body(collector.as_body())
            account.mirror.release(
                lease,
                actual_input=usage.billed_input or None,
                actual_output=usage.output_tokens or None,
                now=now,
            )
            account.inflight -= 1
            await self.gate.wake_one()

            assert classification is not None
            plan.event.attempts.append(
                Attempt(
                    account_id=account.id,
                    status=status,
                    kind=classification.kind,
                    latency=round(latency, 4),
                    detail=classification.detail[:200],
                )
            )

            if classification.kind == "ok":
                await self._settle(account, plan, decision, usage, latency, status or 200)
                return

            account.apply(classification, now, plan.model)
            self.metrics.incr(
                "tokenbiryani_upstream_failures_total",
                account=account.id,
                kind=classification.kind,
            )

            if committed:
                # Past the commit boundary: the client has content. Report, don't retry.
                plan.event.error = classification.kind
                plan.event.status = status
                plan.event.latency = time.time() - plan.started
                self.events.record(plan.event)
                yield sse.error_event(
                    classification.detail or classification.kind, classification.kind
                )
                return

            if (
                classification.request_action is RequestAction.RETURN
                or not self._may_retry(plan, account, classification)
            ):
                message = classification.detail or classification.kind
                self._fail(plan, GatewayError(status or 502, message, classification.kind))
                yield sse.error_event(message, classification.kind)
                return

            await self._backoff(plan)

    def _may_retry(
        self, plan: _Plan, account: AccountRuntime, classification: Classification
    ) -> bool:
        if not classification.retryable:
            return False
        if classification.request_action is RequestAction.RETRY_OTHER:
            if account.id not in plan.excluded:
                plan.excluded.append(account.id)
        plan.attempts += 1
        retry = self.config.retry
        if plan.attempts >= retry.max_attempts:
            return False
        if len(plan.excluded) >= min(retry.max_accounts, len(self.accounts)):
            return False
        if time.time() >= plan.deadline:
            return False
        return True

    async def _backoff(self, plan: _Plan) -> None:
        retry = self.config.retry
        delay = min(
            retry.backoff_max_seconds,
            retry.backoff_base_seconds * (2 ** max(0, plan.attempts - 1)),
        )
        await asyncio.sleep(random.uniform(0, delay))

    async def _settle(
        self,
        account: AccountRuntime,
        plan: _Plan,
        decision: Decision,
        usage: Usage,
        latency: float,
        status: int,
    ) -> None:
        """Record a success: stats, affinity, spend, and the event."""
        now = time.time()
        price = self.config.price_for(plan.model)
        cost = account.record_success(latency, usage, price, now)

        if decision.affinity_broken:
            self.metrics.incr("tokenbiryani_cache_breaks_total", model=plan.model)
        await self.store.set_affinity(
            plan.session, account.id, self.config.routing.affinity_ttl_seconds
        )

        if cost is not None:
            await self.store.add_spend(plan.key.name, cost)

        saved = None
        if price is not None and usage.cache_read_tokens:
            saved = usage.cache_read_tokens * (price.input - price.cache_read) / 1_000_000.0

        event = plan.event
        event.account_id = account.id
        event.status = status
        event.latency = round(now - plan.started, 4)
        event.input_tokens = usage.input_tokens
        event.output_tokens = usage.output_tokens
        event.cache_read_tokens = usage.cache_read_tokens
        event.cache_creation_tokens = usage.cache_creation_tokens
        event.cost_usd = None if cost is None else round(cost, 6)
        event.saved_usd = None if saved is None else round(saved, 6)
        event.affinity_broken = decision.affinity_broken
        event.affinity_honored = decision.affinity_honored
        self.events.record(event)

        self.metrics.incr("tokenbiryani_responses_total", account=account.id, status=str(status))
        self.metrics.incr(
            "tokenbiryani_tokens_total", usage.input_tokens, account=account.id, kind="input"
        )
        self.metrics.incr(
            "tokenbiryani_tokens_total", usage.output_tokens, account=account.id, kind="output"
        )
        self.metrics.incr(
            "tokenbiryani_tokens_total",
            usage.cache_read_tokens,
            account=account.id,
            kind="cache_read",
        )
        if len(event.attempts) > 1:
            self.metrics.incr("tokenbiryani_failovers_total", model=plan.model)

    def _fail(self, plan: _Plan, error: GatewayError) -> None:
        plan.event.status = error.status
        plan.event.error = error.kind
        plan.event.latency = round(time.time() - plan.started, 4)
        self.events.record(plan.event)
        self.metrics.incr("tokenbiryani_gateway_errors_total", kind=error.kind)

    async def simple_request(
        self,
        method: str,
        path: str,
        key: KeyConfig,
        headers: Mapping[str, str],
        body: Optional[Dict[str, Any]] = None,
    ) -> Completion:
        """Auxiliary endpoints (models, count_tokens): no leases, one failover hop.

        These cost no quota, so they skip admission and scoring entirely — running
        them through the router would be ceremony without a purpose.
        """
        now = time.time()
        pool = set(key.pool) if key.pool else None
        candidates = [
            a
            for a in self.accounts.values()
            if (pool is None or a.id in pool) and a.state(now) is not AccountState.DISABLED
        ]
        if not candidates:
            raise GatewayError(503, "no account available", kind="overloaded_error")

        last: Optional[GatewayError] = None
        for account in candidates[: self.config.retry.max_accounts]:
            upstream = self.upstreams[account.id]
            try:
                if method.upper() == "GET":
                    response = await self.client.get(
                        upstream.url(path), headers=upstream.request_headers(headers)
                    )
                    result = type(
                        "R", (), {"status": response.status_code, "headers": dict(response.headers),
                                  "raw": response.content}
                    )()
                else:
                    sent = await upstream.send(self.client, path, body or {}, headers)
                    result = sent
                account.mirror.update_from_headers(dict(result.headers), time.time())
                if result.status < 500 and result.status != 429:
                    return Completion(
                        status=result.status,
                        headers=_response_headers(result.headers, account.id),
                        content=result.raw,
                        event=RequestEvent(request_id="aux", started_at=now),
                    )
                last = GatewayError(result.status, f"upstream returned {result.status}")
            except Exception as exc:  # noqa: BLE001
                last = GatewayError(502, f"{type(exc).__name__}: {exc}")
        raise last or GatewayError(502, "all accounts failed")

    # ---- introspection -------------------------------------------------------

    def _register_metrics(self) -> None:
        m = self.metrics
        m.declare("tokenbiryani_requests_total", "Requests accepted by the gateway")
        m.declare("tokenbiryani_responses_total", "Successful upstream responses")
        m.declare("tokenbiryani_upstream_failures_total", "Upstream failures by class")
        m.declare("tokenbiryani_gateway_errors_total", "Errors generated by the gateway")
        m.declare("tokenbiryani_failovers_total", "Requests that needed more than one account")
        m.declare("tokenbiryani_cache_breaks_total", "Requests routed away from their cache owner")
        m.declare("tokenbiryani_queued_total", "Requests that waited for capacity")
        m.declare("tokenbiryani_tokens_total", "Tokens by account and kind")

        def headroom_gauge():
            now = time.time()
            for account in self.accounts.values():
                yield ((("account", account.id),), account.mirror.headroom(now))

        def inflight_gauge():
            for account in self.accounts.values():
                yield ((("account", account.id),), float(account.inflight))

        def queue_gauge():
            yield ((), float(self.gate.depth))

        m.register_gauge(
            "tokenbiryani_account_headroom", "Binding headroom fraction per account", headroom_gauge
        )
        m.register_gauge(
            "tokenbiryani_account_inflight", "In-flight requests per account", inflight_gauge
        )
        m.register_gauge("tokenbiryani_queue_depth", "Requests waiting for capacity", queue_gauge)

    def capacity_horizon(self, minutes: int = 60, buckets: int = 12) -> Dict[str, Any]:
        """Projected input-token capacity over the next hour.

        Drawable only because we mirror per-account reset timestamps; a round-robin
        proxy has no idea when anything refills.
        """
        now = time.time()
        step = (minutes * 60.0) / buckets
        series: List[Dict[str, Any]] = []
        for index in range(buckets):
            at = now + index * step
            total = 0
            for account in self.accounts.values():
                if account.state(now) is AccountState.DISABLED:
                    continue
                window = account.mirror.input_tokens
                if window.limit is None:
                    continue
                if window.reset_at is not None and at >= window.reset_at:
                    total += window.limit
                elif window.remaining is not None:
                    total += max(0, window.remaining - account.mirror.reserved_input)
            series.append({"offset_seconds": round(index * step), "input_tokens": total})
        return {"minutes": minutes, "series": series}

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        accounts = [a.snapshot(now) for a in self.accounts.values()]
        ready = [
            a
            for a in self.accounts.values()
            if a.state(now) is AccountState.READY
        ]
        ready_tokens = 0
        for account in ready:
            available = account.mirror.input_tokens.available(
                account.mirror.reserved_input, now
            )
            if available is not None:
                ready_tokens += available
        resets = [
            a.mirror.next_reset(now)
            for a in self.accounts.values()
            if a.mirror.next_reset(now) is not None
        ]
        return {
            "strategy": self.router.strategy,
            "accounts": accounts,
            "pool": {
                "total": len(self.accounts),
                "ready": len(ready),
                "cooling": sum(
                    1 for a in self.accounts.values() if a.state(now) is AccountState.COOLING
                ),
                "disabled": sum(
                    1 for a in self.accounts.values() if a.state(now) is AccountState.DISABLED
                ),
                "ready_input_tokens": ready_tokens,
                "next_reset_seconds": round(min(resets), 1) if resets else None,
            },
            "queue": self.gate.snapshot(),
            "stats": self.events.stats(),
        }


@dataclass
class _Plan:
    body: Dict[str, Any]
    headers: Dict[str, str]
    key: KeyConfig
    model: str
    estimate: TokenEstimate
    session: str
    owner: Optional[str]
    event: RequestEvent
    started: float
    deadline: float
    excluded: List[str] = None  # type: ignore[assignment]
    attempts: int = 0

    def __post_init__(self) -> None:
        if self.excluded is None:
            self.excluded = []


def _safe_json(response: httpx.Response) -> Optional[Dict[str, Any]]:
    try:
        parsed = response.json()
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
