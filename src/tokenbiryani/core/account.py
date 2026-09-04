"""Account runtime: health state machine, rolling stats, and cost accounting."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, Optional, Set

from ..config import AccountConfig, ModelPrice
from ..proxy.errors import AccountAction, Classification
from .breaker import BreakerState, CircuitBreaker
from .limits import LimitMirror, TokenEstimate

WINDOW = 50


class AccountState(str, Enum):
    #: serving traffic, headroom available
    READY = "ready"
    #: waiting on a reset; self-heals, nothing for an operator to do
    COOLING = "cooling"
    #: auth failure, spend cap, or a tripped breaker — needs a look
    DISABLED = "disabled"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @classmethod
    def from_body(cls, body: Optional[Dict[str, Any]]) -> Usage:
        raw = (body or {}).get("usage") or {}

        def get(name: str) -> int:
            try:
                return int(raw.get(name) or 0)
            except (TypeError, ValueError):
                return 0

        return cls(
            input_tokens=get("input_tokens"),
            output_tokens=get("output_tokens"),
            cache_read_tokens=get("cache_read_input_tokens"),
            cache_creation_tokens=get("cache_creation_input_tokens"),
        )

    @property
    def billed_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    def cost(self, price: Optional[ModelPrice]) -> Optional[float]:
        if price is None:
            return None
        return (
            self.input_tokens * price.input
            + self.output_tokens * price.output
            + self.cache_read_tokens * price.cache_read
            + self.cache_creation_tokens * price.cache_write
        ) / 1_000_000.0


@dataclass
class AccountRuntime:
    config: AccountConfig
    mirror: LimitMirror = field(default_factory=LimitMirror)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    cooling_until: float = 0.0
    disabled_reason: Optional[str] = None
    unsupported_models: Set[str] = field(default_factory=set)

    inflight: int = 0
    outcomes: Deque[bool] = field(default_factory=lambda: deque(maxlen=WINDOW))
    latencies: Deque[float] = field(default_factory=lambda: deque(maxlen=WINDOW))

    requests_total: int = 0
    failures_total: int = 0
    error_kinds: Dict[str, int] = field(default_factory=dict)
    spend_usd: float = 0.0
    input_tokens_total: int = 0
    output_tokens_total: int = 0
    cache_read_total: int = 0
    cache_creation_total: int = 0

    @property
    def id(self) -> str:
        return self.config.id

    # ---- state ---------------------------------------------------------------

    def state(self, now: float) -> AccountState:
        if not self.config.enabled:
            return AccountState.DISABLED
        if self.disabled_reason:
            return AccountState.DISABLED
        if self.breaker.state(now) is BreakerState.OPEN:
            return AccountState.DISABLED
        if now < self.cooling_until:
            return AccountState.COOLING
        return AccountState.READY

    def cooling_for(self, now: float) -> Optional[float]:
        """Seconds until this account is usable again, from any known cause."""
        candidates = []
        if now < self.cooling_until:
            candidates.append(self.cooling_until - now)
        if self.breaker.state(now) is BreakerState.OPEN and self.breaker.opened_at is not None:
            candidates.append(self.breaker.opened_at + self.breaker.cooldown_seconds - now)
        reset = self.mirror.next_reset(now)
        if reset is not None and not self.mirror.headroom(now):
            candidates.append(reset)
        return min(candidates) if candidates else None

    def eligible(self, model: str, estimate: TokenEstimate, now: float) -> Optional[str]:
        """Return None if this account may serve the request, else why not."""
        if self.state(now) is AccountState.DISABLED:
            return self.disabled_reason or "disabled"
        if self.state(now) is AccountState.COOLING:
            remaining = self.cooling_for(now) or 0.0
            return f"cooling, {remaining:.0f}s remaining"
        if not self.config.supports_model(model):
            return "model not in account allowlist"
        if model in self.unsupported_models:
            return "model rejected by upstream"
        if self.inflight >= self.config.max_concurrency:
            return f"at max concurrency ({self.config.max_concurrency})"
        if self.config.spend_cap_usd is not None and self.spend_usd >= self.config.spend_cap_usd:
            return "spend cap reached"
        if not self.breaker.allow(now):
            return "circuit open"
        if not self.mirror.can_serve(estimate, now):
            return "insufficient headroom"
        return None

    # ---- outcomes ------------------------------------------------------------

    def apply(self, classification: Classification, now: float, model: str = "") -> None:
        action = classification.account_action
        if action is AccountAction.NONE:
            return
        if action is AccountAction.ERROR_TICK:
            self.breaker.record_failure(now)
        elif action is AccountAction.COOLDOWN:
            seconds = classification.cooldown_seconds
            if seconds is None:
                seconds = self.mirror.next_reset(now) or 60.0
            self.cooling_until = max(self.cooling_until, now + float(seconds))
        elif action is AccountAction.SHORT_COOLDOWN:
            self.cooling_until = max(self.cooling_until, now + 5.0)
            self.breaker.record_failure(now)
        elif action is AccountAction.DISABLE:
            self.disabled_reason = classification.kind
        elif action is AccountAction.MARK_MODEL_UNSUPPORTED and model:
            self.unsupported_models.add(model)
        self.outcomes.append(False)
        self.failures_total += 1
        self.error_kinds[classification.kind] = self.error_kinds.get(classification.kind, 0) + 1

    def record_success(
        self, latency: float, usage: Usage, price: Optional[ModelPrice], now: float
    ) -> Optional[float]:
        self.breaker.record_success()
        self.outcomes.append(True)
        self.latencies.append(latency)
        self.requests_total += 1
        self.input_tokens_total += usage.input_tokens
        self.output_tokens_total += usage.output_tokens
        self.cache_read_total += usage.cache_read_tokens
        self.cache_creation_total += usage.cache_creation_tokens
        cost = usage.cost(price)
        if cost is not None:
            self.spend_usd += cost
        return cost

    # ---- derived stats -------------------------------------------------------

    def error_rate(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(1 for ok in self.outcomes if not ok) / float(len(self.outcomes))

    def p95_latency(self) -> Optional[float]:
        if not self.latencies:
            return None
        ordered = sorted(self.latencies)
        index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return ordered[index]

    def cache_hit_rate(self) -> Optional[float]:
        billed = self.input_tokens_total + self.cache_read_total + self.cache_creation_total
        if billed <= 0:
            return None
        return self.cache_read_total / float(billed)

    def load(self) -> float:
        if self.config.max_concurrency <= 0:
            return 1.0
        return min(1.0, self.inflight / float(self.config.max_concurrency))

    def snapshot(self, now: float) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.config.type,
            "state": self.state(now).value,
            "disabled_reason": self.disabled_reason,
            "cooling_for": (
                round(self.cooling_for(now), 1) if self.cooling_for(now) is not None else None
            ),
            "inflight": self.inflight,
            "priority": self.config.priority,
            "cost_tier": self.config.cost_tier,
            "limits": self.mirror.snapshot(now),
            "error_rate": round(self.error_rate(), 4),
            "p95_latency": (
                round(self.p95_latency(), 3) if self.p95_latency() is not None else None
            ),
            "cache_hit_rate": (
                round(self.cache_hit_rate(), 4) if self.cache_hit_rate() is not None else None
            ),
            "requests_total": self.requests_total,
            "failures_total": self.failures_total,
            "error_kinds": dict(self.error_kinds),
            "spend_usd": round(self.spend_usd, 4),
            "tokens": {
                "input": self.input_tokens_total,
                "output": self.output_tokens_total,
                "cache_read": self.cache_read_total,
                "cache_write": self.cache_creation_total,
            },
        }
