"""The rate-limit mirror, token estimation, and leases.

Every Anthropic response carries the account's remaining budget. Mirroring those
headers gives real-time availability with no probe traffic — this is the routing
signal the whole gateway is built on.

Leases are the correctness half. Without an atomic reservation, N concurrent requests
all read the same "plenty of headroom" and stampede one account into a 429.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

#: Rough bytes-per-token for JSON-serialised request bodies. Deliberately low
#: (pessimistic: it over-counts tokens) so leases err toward reserving too much.
CHARS_PER_TOKEN = 3.5

DEFAULT_MAX_TOKENS = 1024


def parse_reset(value: Optional[str]) -> Optional[float]:
    """Anthropic sends RFC3339. Python 3.8's fromisoformat can't take a trailing Z."""
    if not value:
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


@dataclass
class TokenEstimate:
    input_tokens: int
    output_tokens: int
    #: The caller's own `max_tokens`. `output_tokens` is what gets leased and may be
    #: a prediction well below it; this stays the true upper bound, and it is what
    #: `exceeds_capacity` asks about. "Could this account ever serve the request"
    #: has to be answered against what the caller is allowed to receive, not against
    #: what we expect it to receive.
    output_ceiling: int = 0

    def __post_init__(self) -> None:
        if not self.output_ceiling:
            self.output_ceiling = self.output_tokens

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def estimate_request(
    body: Mapping[str, Any],
    safety_margin: float = 1.15,
    predictor: Optional[Callable[[str, int], int]] = None,
) -> TokenEstimate:
    """Estimate a request's cost without pulling in a tokenizer.

    Input is approximated from the serialised body. Output defaults to the caller's
    own ``max_tokens`` — the only honest upper bound available before the fact — and
    a ``predictor`` may lower it to something the model has actually been returning.
    It may only ever lower it: the ceiling is kept on the estimate either way.
    """
    try:
        serialised = json.dumps(
            {k: v for k, v in body.items() if k not in ("max_tokens", "stream")},
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        serialised = str(body)
    approx_input = int(math.ceil(len(serialised) / CHARS_PER_TOKEN * safety_margin))
    raw_max = body.get("max_tokens")
    try:
        max_tokens = int(raw_max) if raw_max is not None else DEFAULT_MAX_TOKENS
    except (TypeError, ValueError):
        max_tokens = DEFAULT_MAX_TOKENS
    ceiling = max(1, max_tokens)
    leased = ceiling
    if predictor is not None:
        try:
            leased = max(1, min(ceiling, int(predictor(str(body.get("model") or ""), ceiling))))
        except Exception:  # noqa: BLE001 - a bad predictor must not fail a request
            leased = ceiling
    return TokenEstimate(
        input_tokens=max(1, approx_input),
        output_tokens=leased,
        output_ceiling=ceiling,
    )


@dataclass
class LimitWindow:
    """One rate-limit dimension: requests, input tokens, or output tokens."""

    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset_at: Optional[float] = None
    updated_at: Optional[float] = None

    @property
    def known(self) -> bool:
        return self.remaining is not None

    def refilled(self, now: float) -> bool:
        return self.reset_at is not None and now >= self.reset_at

    def available(self, reserved: int, now: float) -> Optional[int]:
        """Projected budget: what the mirror says, minus what is already leased."""
        if self.remaining is None:
            return None
        base = self.remaining
        if self.refilled(now) and self.limit is not None:
            base = self.limit
        return max(0, base - reserved)

    def fraction(self, reserved: int, now: float) -> float:
        """0..1 headroom. Unknown windows read as full — optimistic on first contact."""
        available = self.available(reserved, now)
        if available is None or not self.limit:
            return 1.0
        return max(0.0, min(1.0, available / float(self.limit)))

    def seconds_to_reset(self, now: float) -> Optional[float]:
        if self.reset_at is None:
            return None
        return max(0.0, self.reset_at - now)

    def update(
        self,
        limit: Optional[str],
        remaining: Optional[str],
        reset: Optional[str],
        now: float,
    ) -> None:
        if limit is not None:
            try:
                self.limit = int(limit)
            except (TypeError, ValueError):
                pass
        if remaining is not None:
            try:
                self.remaining = int(remaining)
            except (TypeError, ValueError):
                pass
        parsed_reset = parse_reset(reset)
        if parsed_reset is not None:
            self.reset_at = parsed_reset
        self.updated_at = now


#: The rolling windows a Claude subscription session reports. An API-key account
#: sends {limit, remaining, reset} triples; a subscription sends how much of a
#: rolling window it has spent, and nothing else.
UNIFIED_WINDOWS = ("5h", "7d")

#: The only unified status that means "this request would be refused right now".
#: Anything else — `allowed`, `allowed_warning` — is a request we should still make.
UNIFIED_REJECTED = "rejected"


@dataclass
class UnifiedWindow:
    """One `anthropic-ratelimit-unified-*` window: how much of it is spent.

    A subscription session says `utilization: 0.34`, not "412 requests left", so
    there is no budget to lease against — but `1 - utilization` is a measured
    headroom number, and routing on that is not the same as routing on a guess.
    Leases and the capacity horizon still cannot use it: both need absolute token
    counts, and a fraction cannot be decremented by 4,000 tokens.
    """

    status: Optional[str] = None
    utilization: Optional[float] = None
    reset_at: Optional[float] = None
    updated_at: Optional[float] = None

    @property
    def known(self) -> bool:
        return self.utilization is not None

    @property
    def rejected(self) -> bool:
        return (self.status or "").lower() == UNIFIED_REJECTED

    def headroom(self) -> Optional[float]:
        if self.utilization is None:
            return None
        return max(0.0, min(1.0, 1.0 - self.utilization))

    def seconds_to_reset(self, now: float) -> Optional[float]:
        if self.reset_at is None:
            return None
        return max(0.0, self.reset_at - now)

    def update(
        self,
        status: Optional[str],
        utilization: Optional[str],
        reset: Optional[str],
        now: float,
    ) -> None:
        if status is not None:
            self.status = str(status)
        if utilization is not None:
            try:
                self.utilization = float(utilization)
            except (TypeError, ValueError):
                pass
        parsed_reset = parse_reset(reset)
        if parsed_reset is not None:
            self.reset_at = parsed_reset
        if status is not None or utilization is not None:
            self.updated_at = now


@dataclass
class Lease:
    """An atomic reservation held against an account for the life of one request."""

    account_id: str
    input_tokens: int
    output_tokens: int
    requests: int = 1
    released: bool = False


@dataclass
class LimitMirror:
    """Per-account mirror of the upstream's advertised budget, plus outstanding leases.

    ``observable`` is False for upstreams that report no rate-limit headers. Their
    windows never populate, and an unknown window otherwise reads as full — which
    would make such an account beat every account that honestly reports a partly-used
    budget. Instead they score at ``assumed_headroom`` and are left out of anything
    that claims to know future capacity.
    """

    requests: LimitWindow = field(default_factory=LimitWindow)
    input_tokens: LimitWindow = field(default_factory=LimitWindow)
    output_tokens: LimitWindow = field(default_factory=LimitWindow)

    #: Subscription sessions report these instead of the three windows above. They
    #: are read whatever `observable` says: an account that sends both should be
    #: held to whichever is tighter.
    unified: Dict[str, UnifiedWindow] = field(
        default_factory=lambda: {name: UnifiedWindow() for name in UNIFIED_WINDOWS}
    )

    observable: bool = True
    assumed_headroom: float = 0.5

    reserved_requests: int = 0
    reserved_input: int = 0
    reserved_output: int = 0

    def update_from_headers(self, headers: Mapping[str, str], now: float) -> None:
        lowered = {k.lower(): v for k, v in headers.items()}

        def triple(prefix: str):
            return (
                lowered.get(f"anthropic-ratelimit-{prefix}-limit"),
                lowered.get(f"anthropic-ratelimit-{prefix}-remaining"),
                lowered.get(f"anthropic-ratelimit-{prefix}-reset"),
            )

        for window, prefix in (
            (self.requests, "requests"),
            (self.input_tokens, "input-tokens"),
            (self.output_tokens, "output-tokens"),
        ):
            limit, remaining, reset = triple(prefix)
            window.update(limit, remaining, reset, now)

        for name, rolling in self.unified.items():
            rolling.update(
                lowered.get(f"anthropic-ratelimit-unified-{name}-status"),
                lowered.get(f"anthropic-ratelimit-unified-{name}-utilization"),
                lowered.get(f"anthropic-ratelimit-unified-{name}-reset"),
                now,
            )

    def reserve(self, estimate: TokenEstimate, account_id: str) -> Lease:
        lease = Lease(
            account_id=account_id,
            input_tokens=estimate.input_tokens,
            output_tokens=estimate.output_tokens,
        )
        self.reserved_requests += lease.requests
        self.reserved_input += lease.input_tokens
        self.reserved_output += lease.output_tokens
        return lease

    def release(
        self,
        lease: Lease,
        actual_input: Optional[int] = None,
        actual_output: Optional[int] = None,
        now: Optional[float] = None,
    ) -> None:
        """Give the reservation back, then reconcile the mirror against real usage.

        Reconciliation only matters when the response carried no rate-limit headers
        (an error, a transport failure); when it did, ``update_from_headers`` has
        already replaced these numbers with the upstream's own accounting.
        """
        if lease.released:
            return
        lease.released = True
        self.reserved_requests = max(0, self.reserved_requests - lease.requests)
        self.reserved_input = max(0, self.reserved_input - lease.input_tokens)
        self.reserved_output = max(0, self.reserved_output - lease.output_tokens)

        if now is None:
            return
        for window, actual in (
            (self.input_tokens, actual_input),
            (self.output_tokens, actual_output),
        ):
            current = window.remaining
            if actual is None or current is None or window.updated_at is None:
                continue
            if window.updated_at < now:
                window.remaining = max(0, current - int(actual))

    def can_serve(self, estimate: TokenEstimate, now: float) -> bool:
        # A unified window that says `rejected` is the upstream telling us the next
        # request is a 429. Believing it is cheaper than proving it.
        if self.unified_rejected():
            return False
        checks = (
            (self.requests, self.reserved_requests, 1),
            (self.input_tokens, self.reserved_input, estimate.input_tokens),
            (self.output_tokens, self.reserved_output, estimate.output_tokens),
        )
        for window, reserved, need in checks:
            available = window.available(reserved, now)
            if available is not None and available < need:
                return False
        return True

    def exceeds_capacity(self, estimate: TokenEstimate) -> bool:
        """True when no reset could ever make this account able to serve the request."""
        for window, need in (
            (self.input_tokens, estimate.input_tokens),
            (self.output_tokens, estimate.output_ceiling),
        ):
            if window.limit is not None and need > window.limit:
                return True
        return False

    @property
    def unified_known(self) -> bool:
        return any(window.known for window in self.unified.values())

    def unified_headroom(self) -> Optional[float]:
        """The tightest unified window, or None when none were reported."""
        values: List[float] = []
        for window in self.unified.values():
            headroom = window.headroom()
            if headroom is not None:
                values.append(headroom)
        return min(values) if values else None

    def unified_rejected(self) -> bool:
        return any(window.rejected for window in self.unified.values())

    def headroom(self, now: float) -> float:
        """The binding constraint across every dimension the upstream reported.

        Unobservable used to mean "guess `assumed_headroom` and hope". It only has
        to mean that when the upstream reported nothing at all: a subscription
        session reports unified utilisation, which is a measurement, and a guess
        must never win a comparison against one.
        """
        unified = self.unified_headroom()
        if not self.observable and not self.input_tokens.known:
            return self.assumed_headroom if unified is None else unified
        classic = min(
            self.requests.fraction(self.reserved_requests, now),
            self.input_tokens.fraction(self.reserved_input, now),
            self.output_tokens.fraction(self.reserved_output, now),
        )
        return classic if unified is None else min(classic, unified)

    def next_reset(self, now: float) -> Optional[float]:
        candidates: List[float] = []
        classic: List[Any] = [self.requests, self.input_tokens, self.output_tokens]
        for window in classic + list(self.unified.values()):
            seconds = window.seconds_to_reset(now)
            if seconds is not None:
                candidates.append(seconds)
        return min(candidates) if candidates else None

    def snapshot(self, now: float) -> Dict[str, Any]:
        def window(w: LimitWindow, reserved: int) -> Dict[str, Any]:
            reset_in = w.seconds_to_reset(now)
            return {
                "limit": w.limit,
                "remaining": w.remaining,
                "reserved": reserved,
                "available": w.available(reserved, now),
                "fraction": round(w.fraction(reserved, now), 4),
                "reset_in": None if reset_in is None else round(reset_in, 1),
            }

        def unified(w: UnifiedWindow) -> Dict[str, Any]:
            reset_in = w.seconds_to_reset(now)
            headroom = w.headroom()
            return {
                "status": w.status,
                "utilization": None if w.utilization is None else round(w.utilization, 4),
                "headroom": None if headroom is None else round(headroom, 4),
                "reset_in": None if reset_in is None else round(reset_in, 1),
            }

        return {
            "observable": self.observable,
            "requests": window(self.requests, self.reserved_requests),
            "input_tokens": window(self.input_tokens, self.reserved_input),
            "output_tokens": window(self.output_tokens, self.reserved_output),
            "headroom": round(self.headroom(now), 4),
            # Present but empty for an API-key account, so the console can render
            # one shape and decide what to draw from `unified_known`.
            "unified": {name: unified(w) for name, w in self.unified.items()},
            "unified_known": self.unified_known,
        }
