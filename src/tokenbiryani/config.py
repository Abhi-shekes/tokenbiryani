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
    #: A label for humans. The id stays the stable handle used in logs and metrics.
    name: str = ""
    type: str = "anthropic_api"
    api_key: str = ""
    #: Override only. Empty means "whatever this account type's adapter defaults to",
    #: so a bedrock or vertex account is not silently pointed at api.anthropic.com.
    base_url: str = ""
    priority: float = 0.0
    #: A routing weight, not a price. `cost_tiered` drains low tiers first and
    #: `sticky_headroom` prefers them on placement, but this number never enters
    #: cost accounting: reported spend and every spend cap come from the model
    #: price table alone. Set it to rank accounts, not to describe a rate.
    cost_tier: float = 1.0
    models: List[str] = field(default_factory=lambda: ["*"])
    max_concurrency: int = 16
    spend_cap_usd: Optional[float] = None
    enabled: bool = True
    headers: Dict[str, str] = field(default_factory=dict)
    #: Provider-specific settings — region, project, model_map, and so on. Kept
    #: untyped so a new adapter needs no change to the config schema.
    options: Dict[str, Any] = field(default_factory=dict)
    #: False when this upstream reports no anthropic-ratelimit-* headers. An
    #: unobservable account must not read as "full", or it wins every comparison
    #: against accounts that honestly report a partly-used budget.
    observable_limits: bool = True
    #: Headroom to assume for an unobservable account. Deliberately middling: it
    #: should neither dominate a healthy pool nor be starved by it.
    assumed_headroom: float = 0.5

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

    #: How the output half of a lease is sized. `adaptive` predicts from what each
    #: model has actually been returning and is bounded by the caller's own
    #: `max_tokens`, so it can only ever reserve less; `max_tokens` reserves the
    #: ceiling, which is what this did before the estimator existed.
    output_estimate: str = "adaptive"
    output_estimate_quantile: float = 0.95
    output_estimate_min_samples: int = 20
    output_estimate_window: int = 200
    output_estimate_floor: int = 256
    #: Stop predicting for a model once this fraction of its recent completions have
    #: outrun their lease. A p95 predictor is beaten ~5% of the time by design; this
    #: catches a distribution that has changed shape.
    output_estimate_max_undershoot: float = 0.20


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
    #: Required to reach /admin/*. A tenant key must not be able to read the pool's
    #: account ids and spend, let alone mint more keys.
    admin: bool = False

    def supports_model(self, model: str) -> bool:
        for pattern in self.models:
            if pattern == "*" or pattern == model:
                return True
            if pattern.endswith("*") and model.startswith(pattern[:-1]):
                return True
        return False


@dataclass
class ModelPrice:
    """USD per million tokens.

    Costs are only reported, and spend caps only enforced, for models priced here.
    Prices come from one of two places, and never from a literal in this module:
    the operator's own `pricing:` block, or the dated `prices.yaml` that ships with
    the release and is opted into with `pricing: builtin`.

    The rule that produced that split is unchanged — the gateway must never bill
    against a number nobody can attribute. A dated file whose date the console
    displays is attributable; a dict hard-coded here would not be.
    """

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


#: The bundled table, beside this module. Read once, on demand.
BUILTIN_PRICES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices.yaml")

#: Reserved key inside a `pricing:` mapping; no model id collides with it.
BUILTIN = "builtin"


def load_builtin_prices() -> Dict[str, Any]:
    """The shipped price table, or an empty one if it is missing from the install."""
    try:
        with open(BUILTIN_PRICES_PATH, encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except OSError:
        return {}


@dataclass
class StoreConfig:
    """Where shared state lives: affinity, spend, per-key request counts.

    `memory` keeps everything in-process and loses it on restart. `sqlite` persists
    to one file, which is what makes a spend cap mean anything across a restart.
    `redis` shares state between instances.
    """

    backend: str = "memory"
    path: str = "tokenbiryani.db"
    #: Where the encryption key for stored credentials lives. Overridden by the
    #: TOKENBIRYANI_SECRET_KEY environment variable, which containers should use.
    secret_key_path: str = "tokenbiryani.key"
    url: str = "redis://127.0.0.1:6379/0"
    namespace: str = "tokenbiryani"


@dataclass
class CacheConfig:
    """Prompt-cache diagnosis, and the one opt-in that acts on it."""

    #: Rewrite the caller's body to add a `cache_control` breakpoint at the end of
    #: the stable head when it carries none. Off by default: everywhere else this
    #: gateway routes rather than rewrites, and turning this on makes it the third
    #: exception to that after the two fields Bedrock and Vertex need.
    auto_breakpoint: bool = False
    #: Requests per (key, model) before the advisor will express an opinion.
    advice_min_requests: int = 20


@dataclass
class SpendConfig:
    """Spend caps are windowed, not lifetime.

    A lifetime cap on a persistent store would eventually wedge the gateway shut and
    stay that way; a rolling window is what an operator actually means by "cap".
    """

    window_hours: float = 24.0

    @property
    def window_seconds(self) -> float:
        return self.window_hours * 3600.0


@dataclass
class ObservabilityConfig:
    #: prompts are sensitive; never log bodies unless the operator opts in
    log_bodies: bool = False
    event_buffer: int = 500


@dataclass
class OAuthConfig:
    """Where "Log in with Claude" sends people.

    Deliberately empty by default. Anthropic does not publish the OAuth endpoints
    its first-party clients use, and this project has never been run against a real
    subscription session, so a hard-coded guess here would look like a working
    feature and fail confusingly. Setting these three is the operator's decision —
    see docs/oauth.md — and until they are set the login flow refuses with an error
    that says exactly what is missing.
    """

    client_id: str = ""
    authorize_url: str = ""
    token_url: str = ""
    #: Blank means the manual flow: the provider shows a code and the operator pastes
    #: it into the console. Works without registering a callback anywhere.
    redirect_uri: str = ""
    scopes: List[str] = field(default_factory=list)
    #: How far ahead of expiry a session is renewed, and how often that is checked.
    refresh_skew_seconds: float = 300.0
    refresh_interval_seconds: float = 60.0


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    breaker: BreakerConfig = field(default_factory=BreakerConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    spend: SpendConfig = field(default_factory=SpendConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    oauth: OAuthConfig = field(default_factory=OAuthConfig)
    accounts: List[AccountConfig] = field(default_factory=list)
    keys: List[KeyConfig] = field(default_factory=list)
    pricing: Dict[str, ModelPrice] = field(default_factory=dict)
    #: The date on the bundled table, when it is in use. Surfaced by the console so
    #: a stale price is visible rather than silent.
    pricing_as_of: str = ""
    pricing_source: str = ""
    #: Which table the prices above came from: "builtin" (the dated file shipped
    #: with the release), "config" (only what tokenbiryani.yaml names), or "none".
    #: The console can switch this at runtime, which is why the raw block is kept.
    pricing_table: str = "none"
    pricing_raw: Any = None
    path: Optional[str] = None

    def use_pricing(self, table: str) -> str:
        """Rebuild the price table from the config file's own `pricing:` block.

        The operator's entries always win: switching to "builtin" lays the dated
        table underneath them, and switching to "config" takes it away. Neither
        touches the file — a console setting is an overlay, not an edit.
        """
        raw = self.pricing_raw
        prices: Dict[str, ModelPrice] = {}
        as_of, source = "", ""
        if table == BUILTIN:
            bundled = load_builtin_prices()
            as_of = str(bundled.get("as_of") or "")
            source = str(bundled.get("source") or "")
            for name, spec in (bundled.get("models") or {}).items():
                prices[name] = ModelPrice(**spec)
        if isinstance(raw, dict):
            for name, spec in raw.items():
                if name != BUILTIN:
                    prices[name] = ModelPrice(**spec)
        self.pricing = prices
        self.pricing_as_of = as_of
        self.pricing_source = source
        self.pricing_table = table if prices else "none"
        return self.pricing_table

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

        # `pricing: builtin` takes the dated table that ships with the release.
        # A mapping may also set `builtin: true` alongside its own entries, which
        # then override it model by model.
        raw_pricing = raw.get("pricing")
        pricing: Dict[str, ModelPrice] = {}
        as_of, source = "", ""
        wants_builtin = raw_pricing == BUILTIN or (
            isinstance(raw_pricing, dict) and bool(raw_pricing.get(BUILTIN))
        )
        if wants_builtin:
            bundled = load_builtin_prices()
            as_of = str(bundled.get("as_of") or "")
            source = str(bundled.get("source") or "")
            pricing.update(
                {name: build(ModelPrice, spec)
                 for name, spec in (bundled.get("models") or {}).items()}
            )
        elif isinstance(raw_pricing, str):
            raise ConfigError(
                f"pricing: {raw_pricing!r} is not a thing. Use `pricing: builtin` for the "
                "table that ships with this release, or a mapping of model to prices."
            )
        if isinstance(raw_pricing, dict):
            pricing.update(
                {name: build(ModelPrice, spec)
                 for name, spec in raw_pricing.items() if name != BUILTIN}
            )
        accounts = [build(AccountConfig, a) for a in (raw.get("accounts") or [])]
        keys = [build(KeyConfig, k) for k in (raw.get("keys") or [])]

        seen = set()
        for account in accounts:
            if account.id in seen:
                raise ConfigError(f"duplicate account id: {account.id}")
            seen.add(account.id)
            if account.type == "anthropic_api" and not account.api_key:
                raise ConfigError(f"account {account.id} has no api_key")
            if account.type == "oauth" and account.observable_limits:
                # Subscription sessions report no limits. Left true, the account
                # reads as permanently full and wins every routing comparison.
                account.observable_limits = False

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
            store=build(StoreConfig, raw.get("store")),
            spend=build(SpendConfig, raw.get("spend")),
            cache=build(CacheConfig, raw.get("cache")),
            observability=build(ObservabilityConfig, raw.get("observability")),
            oauth=build(OAuthConfig, raw.get("oauth")),
            accounts=accounts,
            keys=keys,
            pricing=pricing,
            pricing_as_of=as_of,
            pricing_source=source,
            pricing_table=(
                BUILTIN if wants_builtin else ("config" if pricing else "none")
            ),
            pricing_raw=raw_pricing if isinstance(raw_pricing, dict) else None,
            path=path,
        )

    @classmethod
    def load(cls, path: str) -> Config:
        with open(path, encoding="utf-8") as handle:
            return cls.from_dict(yaml.safe_load(handle) or {}, path=path)
