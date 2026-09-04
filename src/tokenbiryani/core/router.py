"""Routing: filter, score, pick — and record why every loser lost.

The decision object is deliberately verbose. An operator who cannot see why a request
went where it went has no reason to trust the router, so every candidate carries its
weighted terms and a verdict, and that travels straight through to the inspector.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..config import RoutingWeights
from .account import AccountRuntime
from .limits import TokenEstimate


@dataclass
class StrategySpec:
    """Per-strategy multipliers over the operator's base weights."""

    affinity: float = 1.0
    headroom: float = 1.0
    priority: float = 1.0
    load: float = 1.0
    errors: float = 1.0
    cost: float = 1.0
    rotate: bool = False


STRATEGIES: Dict[str, StrategySpec] = {
    # Affinity first, then headroom. Keeps prompt caches warm; the default.
    "sticky_headroom": StrategySpec(),
    # Pure most-available. Correct for stateless batch traffic, wasteful for chat.
    "headroom": StrategySpec(affinity=0.0),
    # Drain the cheap accounts, spill upward.
    "cost_tiered": StrategySpec(affinity=0.5, headroom=0.25, cost=8.0),
    # Strict ordered failover: primary, then backup.
    "priority": StrategySpec(affinity=0.25, headroom=0.25, priority=8.0),
    # Baseline. Shipped so the default can be benchmarked against it.
    "least_loaded": StrategySpec(affinity=0.0, headroom=0.2, load=8.0),
    # Baseline. Ignores every signal on purpose.
    "round_robin": StrategySpec(
        affinity=0.0, headroom=0.0, priority=0.0, load=0.0, errors=0.0, cost=0.0, rotate=True
    ),
}


@dataclass
class RoutingContext:
    model: str
    estimate: TokenEstimate
    session_key: str
    now: float
    sticky_owner: Optional[str] = None
    pool: Sequence[str] = ()
    exclude: Sequence[str] = ()


@dataclass
class ScoredCandidate:
    account_id: str
    verdict: str
    score: Optional[float] = None
    terms: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "verdict": self.verdict,
            "score": None if self.score is None else round(self.score, 4),
            "terms": {k: round(v, 4) for k, v in self.terms.items()},
        }


@dataclass
class Decision:
    chosen: Optional[AccountRuntime]
    candidates: List[ScoredCandidate]
    strategy: str
    affinity_honored: bool = False
    affinity_broken: bool = False
    #: True when nothing is eligible *now* but something will be after a reset
    retryable_later: bool = False
    soonest_available: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "chosen": self.chosen.id if self.chosen else None,
            "affinity_honored": self.affinity_honored,
            "affinity_broken": self.affinity_broken,
            "soonest_available": (
                round(self.soonest_available, 1) if self.soonest_available is not None else None
            ),
            "candidates": [c.to_dict() for c in self.candidates],
        }


def _jitter(session_key: str, account_id: str) -> float:
    """Deterministic tie-break: stable under replay, spread across sessions."""
    digest = hashlib.sha256((session_key + "|" + account_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / float(1 << 32) * 1e-6


class Router:
    def __init__(self, strategy: str, weights: RoutingWeights) -> None:
        if strategy not in STRATEGIES:
            raise ValueError(
                "unknown routing strategy {!r}; known: {}".format(
                    strategy, ", ".join(sorted(STRATEGIES))
                )
            )
        self.strategy = strategy
        self.spec = STRATEGIES[strategy]
        self.weights = weights
        self._cursor = 0

    def select(
        self, accounts: Iterable[AccountRuntime], ctx: RoutingContext
    ) -> Decision:
        accounts = list(accounts)
        excluded = set(ctx.exclude)
        pool = set(ctx.pool) if ctx.pool else None

        eligible: List[AccountRuntime] = []
        candidates: List[ScoredCandidate] = []
        soonest: Optional[float] = None

        for account in accounts:
            if pool is not None and account.id not in pool:
                candidates.append(
                    ScoredCandidate(account.id, "filtered — outside this key's pool")
                )
                continue
            if account.id in excluded:
                candidates.append(
                    ScoredCandidate(account.id, "filtered — already attempted")
                )
                continue
            if account.mirror.exceeds_capacity(ctx.estimate):
                candidates.append(
                    ScoredCandidate(account.id, "filtered — request exceeds account limit")
                )
                continue
            reason = account.eligible(ctx.model, ctx.estimate, ctx.now)
            if reason is not None:
                candidates.append(ScoredCandidate(account.id, "filtered — " + reason))
                wait = account.cooling_for(ctx.now)
                if wait is None and "headroom" in reason:
                    wait = account.mirror.next_reset(ctx.now)
                if wait is not None:
                    soonest = wait if soonest is None else min(soonest, wait)
                continue
            eligible.append(account)

        if not eligible:
            return Decision(
                None,
                candidates,
                self.strategy,
                retryable_later=soonest is not None,
                soonest_available=soonest,
            )

        tiers = [a.config.cost_tier for a in eligible]
        low, high = min(tiers), max(tiers)
        spread = (high - low) or 1.0

        scored: List[Any] = []
        for index, account in enumerate(eligible):
            terms = self._terms(account, ctx, index, len(eligible), low, spread)
            total = sum(terms.values()) + _jitter(ctx.session_key, account.id)
            scored.append((total, account, terms))

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best, best_terms = scored[0]

        for total, account, terms in scored:
            candidates.append(
                ScoredCandidate(
                    account.id,
                    "chosen" if account is best else "eligible",
                    score=total,
                    terms=terms,
                )
            )

        if self.spec.rotate:
            self._cursor = (self._cursor + 1) % max(1, len(eligible))

        honored = bool(ctx.sticky_owner) and ctx.sticky_owner == best.id
        broken = bool(ctx.sticky_owner) and ctx.sticky_owner != best.id
        return Decision(
            best,
            candidates,
            self.strategy,
            affinity_honored=honored,
            affinity_broken=broken,
        )

    def _terms(
        self,
        account: AccountRuntime,
        ctx: RoutingContext,
        index: int,
        count: int,
        low_tier: float,
        tier_spread: float,
    ) -> Dict[str, float]:
        w, s = self.weights, self.spec
        if s.rotate:
            # Distance forward from the cursor; the account at the cursor scores highest.
            offset = (index - self._cursor) % max(1, count)
            return {"rotation": -float(offset)}

        is_owner = 1.0 if ctx.sticky_owner == account.id else 0.0
        normalized_cost = (account.config.cost_tier - low_tier) / tier_spread
        return {
            "affinity": w.affinity * s.affinity * is_owner,
            "headroom": w.headroom * s.headroom * account.mirror.headroom(ctx.now),
            "priority": w.priority * s.priority * account.config.priority,
            "load": -w.load * s.load * account.load(),
            "errors": -w.errors * s.errors * account.error_rate(),
            "cost": -w.cost * s.cost * normalized_cost,
        }
