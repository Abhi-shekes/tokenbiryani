"""Configuration: a single tokenbiryani.yaml, with ${ENV} interpolation and hot reload."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised when tokenbiryani.yaml is missing something required or self-contradictory."""


def interpolate(value: Any) -> Any:
    """Expand ${VAR} and ${VAR:-default} anywhere in a nested structure."""
    if isinstance(value, str):

        def sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            got = os.environ.get(name)
            if got is None:
                if default is None:
                    raise ConfigError(
                        f"config references ${{{name}}} but that environment variable is not set"
                    )
                return default
            return got

        return _ENV_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v) for v in value]
    return value


@dataclass
class AccountConfig:
    id: str
    type: str = "anthropic_api"
    api_key: str = ""
    base_url: str = "https://api.anthropic.com"
    priority: float = 0.0
    cost_tier: float = 1.0
    models: List[str] = field(default_factory=lambda: ["*"])
    max_concurrency: int = 16
    spend_cap_usd: Optional[float] = None
    enabled: bool = True
    headers: Dict[str, str] = field(default_factory=dict)

    def supports_model(self, model: str) -> bool:
        for pattern in self.models:
            if pattern == "*" or pattern == model:
                return True
            if pattern.endswith("*") and model.startswith(pattern[:-1]):
                return True
        return False


@dataclass
class RoutingWeights:
    affinity: float = 0.40
    headroom: float = 0.40
    priority: float = 0.10
    load: float = 0.15
    errors: float = 0.20
    cost: float = 0.10


@dataclass
class RoutingConfig:
    strategy: str = "sticky_headroom"
    weights: RoutingWeights = field(default_factory=RoutingWeights)
    affinity_ttl_seconds: float = 1800.0
    #: multiplied into every token estimate before it becomes a lease
    estimate_safety_margin: float = 1.15


@dataclass
class RetryConfig:
    max_attempts: int = 3
    max_accounts: int = 3
    deadline_seconds: float = 120.0
    backoff_base_seconds: float = 0.25
    backoff_max_seconds: float = 8.0
    #: how long to hold a stream before the first content delta, waiting to see if it fails
    first_token_grace_seconds: float = 20.0


@dataclass
class BreakerConfig:
    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    overloaded_cooldown_seconds: float = 5.0


@dataclass
class BatchConfig:
    """Spill lane: batch-priority work can go to the Message Batches API.

    Off by default. Batches are cheaper but asynchronous, so a request that spills
    holds its connection open while the gateway polls — bounded by the request's own
    wait budget, never longer.
    """

    enabled: bool = False
    poll_interval_seconds: float = 2.0
    #: Applied to the computed cost of a spilled request. Anthropic prices batch work
    #: below standard; the exact figure is the operator's to supply, like `pricing`.
    cost_multiplier: float = 0.5


@dataclass
class QueueConfig:
    max_size: int = 128
    default_max_wait_seconds: float = 60.0


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    allow_remote: bool = False
    request_timeout_seconds: float = 600.0
    #: Re-read the config file when its mtime changes. 0 disables polling; the
    #: /admin/reload endpoint still works either way.
    hot_reload_seconds: float = 5.0


@dataclass
class KeyConfig:
    key: str
    name: str = "default"
    models: List[str] = field(default_factory=lambda: ["*"])
    pool: List[str] = field(default_factory=list)
    rpm: Optional[int] = None
    spend_cap_usd: Optional[float] = None
    #: "interactive" (default) or "batch". Batch traffic yields the queue to
    #: interactive traffic when the pool is saturated.
    priority: str = "interactive"
    #: How long this key's requests will wait for capacity before being told to
    #: come back. Falls back to queue.default_max_wait_seconds.
    max_wait_seconds: Optional[float] = None

    def supports_model(self, model: str) -> bool:
        for pattern in self.models:
            if pattern == "*" or pattern == model:
                return True
            if pattern.endswith("*") and model.startswith(pattern[:-1]):
                return True
        return False


@dataclass
class ModelPrice:
    """USD per million tokens. Operator-supplied: the gateway ships no price list.

    Costs are only reported, and spend caps only enforced, for models named here.
    Guessing prices in code would mean silently billing against stale numbers.
    """

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


@dataclass
class ObservabilityConfig:
    #: prompts are sensitive; never log bodies unless the operator opts in
    log_bodies: bool = False
    event_buffer: int = 500


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    breaker: BreakerConfig = field(default_factory=BreakerConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    accounts: List[AccountConfig] = field(default_factory=list)
    keys: List[KeyConfig] = field(default_factory=list)
    pricing: Dict[str, ModelPrice] = field(default_factory=dict)
    path: Optional[str] = None

    def price_for(self, model: str) -> Optional[ModelPrice]:
        if model in self.pricing:
            return self.pricing[model]
        for pattern, price in self.pricing.items():
            if pattern.endswith("*") and model.startswith(pattern[:-1]):
                return price
        return None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], path: Optional[str] = None) -> Config:
        raw = interpolate(raw or {})

        def build(klass, data):
            if not data:
                return klass()
            fields = {f for f in klass.__dataclass_fields__}
            unknown = set(data) - fields
            if unknown:
                raise ConfigError(
                    f"unknown {klass.__name__} option(s): {', '.join(sorted(unknown))}"
                )
            return klass(**data)

        routing_raw = dict(raw.get("routing") or {})
        weights = build(RoutingWeights, routing_raw.pop("weights", None))
        routing = build(RoutingConfig, routing_raw)
        routing.weights = weights

        pricing = {
            name: build(ModelPrice, spec) for name, spec in (raw.get("pricing") or {}).items()
        }
        accounts = [build(AccountConfig, a) for a in (raw.get("accounts") or [])]
        keys = [build(KeyConfig, k) for k in (raw.get("keys") or [])]

        seen = set()
        for account in accounts:
            if account.id in seen:
                raise ConfigError(f"duplicate account id: {account.id}")
            seen.add(account.id)
            if account.type == "anthropic_api" and not account.api_key:
                raise ConfigError(f"account {account.id} has no api_key")

        for key in keys:
            for account_id in key.pool:
                if account_id not in seen:
                    raise ConfigError(
                        f"key {key.name} scopes to unknown account {account_id!r}"
                    )

        return cls(
            server=build(ServerConfig, raw.get("server")),
            routing=routing,
            retry=build(RetryConfig, raw.get("retry")),
            breaker=build(BreakerConfig, raw.get("breaker")),
            queue=build(QueueConfig, raw.get("queue")),
            batch=build(BatchConfig, raw.get("batch")),
            observability=build(ObservabilityConfig, raw.get("observability")),
            accounts=accounts,
            keys=keys,
            pricing=pricing,
            path=path,
        )

    @classmethod
    def load(cls, path: str) -> Config:
        with open(path, encoding="utf-8") as handle:
            return cls.from_dict(yaml.safe_load(handle) or {}, path=path)
