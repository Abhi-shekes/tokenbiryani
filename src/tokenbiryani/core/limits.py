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
from typing import Any, Dict, Mapping, Optional

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

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def estimate_request(body: Mapping[str, Any], safety_margin: float = 1.15) -> TokenEstimate:
    """Estimate a request's cost without pulling in a tokenizer.

    Input is approximated from the serialised body; output is the caller's own
    ``max_tokens``, which is the only honest upper bound we have before the fact.
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
    return TokenEstimate(input_tokens=max(1, approx_input), output_tokens=max(1, max_tokens))


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
    """Per-account mirror of the upstream's advertised budget, plus outstanding leases."""

    requests: LimitWindow = field(default_factory=LimitWindow)
    input_tokens: LimitWindow = field(default_factory=LimitWindow)
    output_tokens: LimitWindow = field(default_factory=LimitWindow)

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
            (self.output_tokens, estimate.output_tokens),
        ):
            if window.limit is not None and need > window.limit:
                return True
        return False

    def headroom(self, now: float) -> float:
        """The binding constraint across all three dimensions."""
        return min(
            self.requests.fraction(self.reserved_requests, now),
            self.input_tokens.fraction(self.reserved_input, now),
            self.output_tokens.fraction(self.reserved_output, now),
        )

    def next_reset(self, now: float) -> Optional[float]:
        candidates = []
        for window in (self.requests, self.input_tokens, self.output_tokens):
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

        return {
            "requests": window(self.requests, self.reserved_requests),
            "input_tokens": window(self.input_tokens, self.reserved_input),
            "output_tokens": window(self.output_tokens, self.reserved_output),
            "headroom": round(self.headroom(now), 4),
        }
