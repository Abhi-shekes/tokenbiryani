"""The gateway: admit, route, lease, proxy, reconcile, recover.

This is the seven-stage pipeline from the plan. The two subtleties worth reading for
are the lease lifecycle (reserve before dispatch, reconcile against real usage) and
the streaming commit boundary in ``stream`` — the single point where a failure stops
being hideable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple

import httpx

from ..config import Config, ConfigError, KeyConfig
from ..observability.events import Attempt, EventLog, RequestEvent
from ..observability.metrics import Metrics
from ..providers.anthropic_api import build_upstream
from ..providers.base import BLOCKED_RESPONSE_HEADERS, Upstream
from ..proxy import sse
from ..proxy.errors import Classification, RequestAction, classify, classify_exception
from ..store.base import SCOPE_ACCOUNT, SCOPE_KEY, StateStore, build_store
from . import batch as batch_lane
from .account import AccountRuntime, AccountState, Usage
from .breaker import CircuitBreaker
from .keys import KeyRegistry, generate_key, record_from_config
from .limits import TokenEstimate, estimate_request
from .queue import (
    MAX_WAIT_HEADER,
    PRIORITY_BATCH,
    PRIORITY_HEADER,
    CapacityGate,
    QueueFull,
    admission_check,
    parse_priority,
    priority_name,
)
from .router import Decision, Router, RoutingContext
from .session import session_key as compute_session_key

MESSAGES_PATH = "/v1/messages"

logger = logging.getLogger("tokenbiryani")


class GatewayError(Exception):
    """A failure the gateway itself is reporting, in Anthropic's error shape."""

    def __init__(
        self,
        status: int,
        message: str,
        kind: str = "api_error",
        retry_after: Optional[float] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.kind = kind
        self.retry_after = retry_after
        self.extra_headers = extra_headers or {}

    def payload(self) -> Dict[str, Any]:
        return {"type": "error", "error": {"type": self.kind, "message": self.message}}

    def headers(self) -> Dict[str, str]:
        headers = dict(self.extra_headers)
        if self.retry_after is not None:
            headers["retry-after"] = str(int(max(1, round(self.retry_after))))
        return headers


class _SpillToBatch(Exception):
    """Internal: the pool is saturated and this request is allowed to spill."""


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
        self.store = store or build_store(config)
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
        await self.store.startup()
        await self.hydrate_spend()
        await self.refresh_keys()

    async def hydrate_spend(self) -> None:
        """Load each account's windowed spend from the store.

        Account caps are checked on the synchronous routing path, so the figure is
        cached in memory. Without this, a restart would hand every capped account a
        clean slate — which is the whole reason the ledger is persistent.
        """
        totals = await self.store.spend_by_scope(SCOPE_ACCOUNT, self.config.spend.window_seconds)
        for account_id, total in totals.items():
            account = self.accounts.get(account_id)
            if account is not None:
                account.spend_usd = total

    async def resync_spend(self, interval: float = 60.0) -> None:
        """Re-read spend and managed keys so this instance cannot drift from the store."""
        while True:
            await asyncio.sleep(interval)
            try:
                await self.hydrate_spend()
                await self.refresh_keys()
            except Exception as exc:  # noqa: BLE001 - a store blip must not kill routing
                logger.warning("state resync failed: %s", exc)

    async def refresh_keys(self) -> None:
        """Load managed keys from the store into the registry."""
        self.keys.set_managed(await self.store.list_keys())

    async def create_key(self, name: str, **options: Any) -> Tuple[str, Dict[str, Any]]:
        """Mint a managed key. The plaintext is returned once and never stored."""
        name = (name or "").strip()
        if not name:
            raise GatewayError(400, "a key needs a name", kind="invalid_request_error")
        await self.refresh_keys()
        if self.keys.by_name(name) is not None:
            raise GatewayError(
                409, f"a key named {name!r} already exists", kind="invalid_request_error"
            )

        pool = list(options.get("pool") or [])
        unknown = [account_id for account_id in pool if account_id not in self.accounts]
        if unknown:
            raise GatewayError(
                400,
                "unknown account(s) in pool: {}".format(", ".join(sorted(unknown))),
                kind="invalid_request_error",
            )

        plaintext = generate_key()
        template = KeyConfig(
            key="",
            name=name,
            models=list(options.get("models") or ["*"]),
            pool=pool,
            rpm=options.get("rpm"),
            spend_cap_usd=options.get("spend_cap_usd"),
            priority=str(options.get("priority") or "interactive"),
            max_wait_seconds=options.get("max_wait_seconds"),
            admin=bool(options.get("admin")),
        )
        record = record_from_config(template, plaintext)
        await self.store.put_key(record)
        await self.refresh_keys()
        redacted = {k: v for k, v in record.items() if k != "key_hash"}
        return plaintext, redacted

    async def revoke_key(self, name: str) -> bool:
        """Revoke a managed key. Config keys belong to the file, not to the API."""
        if self.keys.is_config_key(name):
            raise GatewayError(
                409,
                f"{name!r} is declared in the config file; remove it there and reload",
                kind="invalid_request_error",
            )
        removed = await self.store.delete_key(name)
        await self.refresh_keys()
        return removed

    def reload(self, config: Config) -> Dict[str, Any]:
        """Swap in a new config without dropping in-flight state.

        Accounts that survive the reload keep their limit mirror, breaker and
        counters — a config edit must not hand every account a clean bill of health
        and re-stampede an upstream that was cooling for a good reason.
        """
        before = set(self.accounts)
        after = [a.id for a in config.accounts]

        for account_config in config.accounts:
            existing = self.accounts.get(account_config.id)
            if existing is None:
                self.accounts[account_config.id] = AccountRuntime(
                    config=account_config,
                    breaker=CircuitBreaker(
                        failure_threshold=config.breaker.failure_threshold,
                        cooldown_seconds=config.breaker.cooldown_seconds,
                    ),
                )
                self.upstreams[account_config.id] = build_upstream(account_config)
                continue
            credentials_changed = (
                existing.config.api_key != account_config.api_key
                or existing.config.base_url != account_config.base_url
                or existing.config.type != account_config.type
            )
            existing.config = account_config
            existing.breaker.failure_threshold = config.breaker.failure_threshold
            existing.breaker.cooldown_seconds = config.breaker.cooldown_seconds
            if credentials_changed:
                self.upstreams[account_config.id] = build_upstream(account_config)
                # New credentials deserve a fresh chance: a rotated key is the usual
                # reason an account was disabled in the first place.
                existing.disabled_reason = None
                existing.breaker.record_success()

        removed = [account_id for account_id in before if account_id not in after]
        for account_id in removed:
            self.accounts.pop(account_id, None)
            self.upstreams.pop(account_id, None)

        managed = list(self.keys._managed)  # noqa: SLF001 - same object's own state
        self.config = config
        self.keys = KeyRegistry(config.keys)
        self.keys.set_managed(managed)
        self.router = Router(config.routing.strategy, config.routing.weights)
        self.gate.max_size = config.queue.max_size
        self.gate.default_max_wait = config.queue.default_max_wait_seconds

        added = [account_id for account_id in after if account_id not in before]
        return {
            "accounts": after,
            "added": added,
            "removed": removed,
            "strategy": self.router.strategy,
        }

    def reload_from_path(self) -> Dict[str, Any]:
        """Re-read the config file. A broken file leaves the running config alone."""
        if not self.config.path:
            raise GatewayError(
                400, "gateway was not started from a config file", kind="invalid_request_error"
            )
        try:
            fresh = Config.load(self.config.path)
        except (OSError, ConfigError) as exc:
            raise GatewayError(
                400, f"config reload failed, keeping the running config: {exc}",
                kind="invalid_request_error",
            ) from exc
        return self.reload(fresh)

    async def watch_config(self) -> None:
        """Poll the config file's mtime and reload when it moves."""
        interval = self.config.server.hot_reload_seconds
        if not self.config.path or interval <= 0:
            return
        try:
            last = os.path.getmtime(self.config.path)
        except OSError:
            return
        while True:
            await asyncio.sleep(interval)
            try:
                current = os.path.getmtime(self.config.path)
            except OSError:
                continue
            if current == last:
                continue
            last = current
            try:
                result = self.reload_from_path()
            except GatewayError as exc:
                logger.warning("config reload rejected: %s", exc.message)
                continue
            logger.info("config reloaded: %s", json.dumps(result))

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        await self.store.close()

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
            try:
                account, decision = await self._acquire(plan)
            except _SpillToBatch:
                return await self._spill(plan)
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
            spent = await self.store.get_spend(
                SCOPE_KEY, key.name, self.config.spend.window_seconds
            )
            if spent >= key.spend_cap_usd:
                raise GatewayError(
                    429,
                    f"key {key.name!r} has spent ${spent:.2f} of its "
                    f"${key.spend_cap_usd:.2f} cap in the last "
                    f"{self.config.spend.window_hours:.0f}h",
                    kind="rate_limit_error",
                )

        lowered = {k.lower(): v for k, v in headers.items()}
        priority = parse_priority(
            lowered.get(PRIORITY_HEADER), parse_priority(key.priority)
        )
        max_wait = _positive_float(
            lowered.get(MAX_WAIT_HEADER),
            key.max_wait_seconds
            if key.max_wait_seconds is not None
            else self.config.queue.default_max_wait_seconds,
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
        event.priority = priority_name(priority)
        self.metrics.incr(
            "tokenbiryani_requests_total",
            model=model,
            key=key.name,
            priority=priority_name(priority),
        )
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
            priority=priority,
            max_wait=max_wait,
        )

    async def _acquire(self, plan: _Plan) -> Tuple[AccountRuntime, Decision]:
        """Route, or wait on the queue for capacity that is known to be coming."""
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
                plan.event.queued_for = round(plan.queued_seconds, 3)
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

            if self._may_spill(plan):
                raise _SpillToBatch()

            remaining_wait = plan.max_wait - plan.queued_seconds
            if remaining_wait <= 0:
                error = GatewayError(
                    429,
                    f"waited {plan.max_wait:.0f}s for pool capacity without any becoming "
                    "available",
                    kind="rate_limit_error",
                    retry_after=decision.soonest_available,
                )
                self._fail(plan, error)
                raise error

            budget = min(
                plan.deadline - now,
                remaining_wait,
                admission.retry_after + 0.25,
            )
            self.metrics.incr(
                "tokenbiryani_queued_total",
                model=plan.model,
                priority=priority_name(plan.priority),
            )
            waited_from = time.time()
            try:
                await self.gate.wait(plan.priority, budget)
            except QueueFull as exc:
                error = GatewayError(
                    429,
                    str(exc),
                    kind="rate_limit_error",
                    retry_after=decision.soonest_available,
                )
                self._fail(plan, error)
                raise error from exc
            finally:
                plan.queued_seconds += time.time() - waited_from

    def _may_spill(self, plan: _Plan) -> bool:
        return (
            self.config.batch.enabled
            and plan.priority >= PRIORITY_BATCH
            and not plan.event.streamed
            and not plan.spilled
            and self._batch_account(plan) is not None
        )

    def _batch_account(self, plan: _Plan) -> Optional[AccountRuntime]:
        """Batches have their own upstream rate-limit pool, so a messages-saturated
        account is still a fine place to send one. Prefer the cache owner."""
        pool = set(plan.key.pool) if plan.key.pool else None
        candidates = [
            account
            for account in self.accounts.values()
            if (pool is None or account.id in pool)
            and account.state(time.time()) is not AccountState.DISABLED
            and account.config.supports_model(plan.model)
            and plan.model not in account.unsupported_models
        ]
        if not candidates:
            return None
        for account in candidates:
            if account.id == plan.owner:
                return account
        return candidates[0]

    async def _spill(self, plan: _Plan) -> Completion:
        """Run a saturated batch-priority request through the Batches API."""
        plan.spilled = True
        account = self._batch_account(plan)
        if account is None:
            raise GatewayError(503, "no account can accept a batch", kind="overloaded_error")

        deadline = min(
            plan.deadline, time.time() + max(0.0, plan.max_wait - plan.queued_seconds)
        )
        started = time.time()
        self.metrics.incr("tokenbiryani_batch_spills_total", account=account.id)
        try:
            outcome = await batch_lane.run_single(
                self.client,
                self.upstreams[account.id],
                plan.body,
                plan.headers,
                self.config.batch.poll_interval_seconds,
                deadline,
            )
        except batch_lane.BatchTimeout as exc:
            self.metrics.incr("tokenbiryani_batch_timeouts_total", account=account.id)
            error = GatewayError(
                429,
                "batch did not finish within this request's wait budget; it was "
                "cancelled upstream",
                kind="rate_limit_error",
                extra_headers=(
                    {"x-tokenbiryani-batch-id": exc.batch_id} if exc.batch_id else None
                ),
            )
            self._fail(plan, error)
            raise error from exc
        except (batch_lane.BatchUnavailable, httpx.HTTPError) as exc:
            # The spill lane is an optimisation, never a dependency: fall back to
            # the normal queue rather than failing the request.
            self.metrics.incr("tokenbiryani_batch_fallbacks_total", account=account.id)
            logger.info("batch spill unavailable, falling back to queue: %s", exc)
            plan.event.attempts.append(
                Attempt(account_id=account.id, kind="batch_unavailable", detail=str(exc)[:200])
            )
            return await self._resume_after_spill(plan)

        latency = time.time() - started
        usage = Usage.from_body(outcome.message)
        plan.event.via = "batch"
        plan.event.batch_id = outcome.batch_id
        plan.event.attempts.append(
            Attempt(
                account_id=account.id,
                status=200,
                kind="batch",
                latency=round(latency, 4),
                detail=f"{outcome.polls} polls",
            )
        )
        decision = Decision(account, [], "batch_spill")
        await self._settle(
            account, plan, decision, usage, latency, 200,
            cost_multiplier=self.config.batch.cost_multiplier,
        )
        return Completion(
            status=200,
            headers={
                "content-type": "application/json",
                "x-tokenbiryani-account": account.id,
                "x-tokenbiryani-via": "batch",
                "x-tokenbiryani-batch-id": outcome.batch_id,
            },
            content=json.dumps(outcome.message).encode("utf-8"),
            event=plan.event,
        )

    async def _resume_after_spill(self, plan: _Plan) -> Completion:
        """Re-enter the normal path once, with spilling disabled for this request."""
        account, decision = await self._acquire(plan)
        result, classification, latency = await self._dispatch(account, plan, MESSAGES_PATH)
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
        error = GatewayError(
            502 if result is None else result.status,
            classification.detail or classification.kind,
            kind=classification.kind,
        )
        self._fail(plan, error)
        raise error

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
                    status = int(response.status_code)
                    if status != 200:
                        await response.aread()
                        body = _safe_json(response)
                        classification = classify(status, dict(response.headers), body)
                        account.mirror.update_from_headers(dict(response.headers), time.time())
                    else:
                        account.mirror.update_from_headers(dict(response.headers), time.time())
                        buffered: List[bytes] = []
                        grace = started + self.config.retry.first_token_grace_seconds
                        async for chunk in upstream.iter_sse(response):
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
        cost_multiplier: float = 1.0,
    ) -> None:
        """Record a success: stats, affinity, spend, and the event."""
        now = time.time()
        price = self.config.price_for(plan.model)
        cost = account.record_success(latency, usage, price, now, cost_multiplier)

        if decision.affinity_broken:
            self.metrics.incr("tokenbiryani_cache_breaks_total", model=plan.model)
        await self.store.set_affinity(
            plan.session, account.id, self.config.routing.affinity_ttl_seconds
        )

        if cost is not None:
            await self.store.add_spend(SCOPE_KEY, plan.key.name, cost)
            await self.store.add_spend(SCOPE_ACCOUNT, account.id, cost)

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
        m.declare("tokenbiryani_batch_spills_total", "Requests diverted to the Batches API")
        m.declare("tokenbiryani_batch_timeouts_total", "Spilled batches that outlived their budget")
        m.declare("tokenbiryani_batch_fallbacks_total", "Spills that fell back to the queue")
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
        resets = []
        for account in self.accounts.values():
            reset = account.mirror.next_reset(now)
            if reset is not None:
                resets.append(reset)
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
    priority: int = 0
    max_wait: float = 60.0
    spilled: bool = False
    queued_seconds: float = 0.0
    excluded: List[str] = None  # type: ignore[assignment]
    attempts: int = 0

    def __post_init__(self) -> None:
        if self.excluded is None:
            self.excluded = []


def _positive_float(raw: Optional[str], fallback: float) -> float:
    """A client may shorten its own wait budget, never extend it past the operator's."""
    if raw is None:
        return fallback
    try:
        requested = float(raw)
    except (TypeError, ValueError):
        return fallback
    if requested <= 0:
        return 0.0
    return min(requested, fallback)


def _safe_json(response: httpx.Response) -> Optional[Dict[str, Any]]:
    try:
        parsed = response.json()
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
