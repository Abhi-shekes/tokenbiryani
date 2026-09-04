"""Routing: who is eligible, who wins, and why the losers lost."""

from __future__ import annotations

import time

from conftest import make_config

from tokenbiryani.config import RoutingWeights
from tokenbiryani.core.account import AccountRuntime
from tokenbiryani.core.limits import TokenEstimate
from tokenbiryani.core.router import Router, RoutingContext


def runtimes(config):
    return [AccountRuntime(config=a) for a in config.accounts]


def ctx(**kwargs):
    defaults = dict(
        model="claude-test-1",
        estimate=TokenEstimate(100, 100),
        session_key="fp:abc",
        now=time.time(),
    )
    defaults.update(kwargs)
    return RoutingContext(**defaults)


def test_selection_is_deterministic_for_a_session():
    config = make_config(["a", "b", "c"])
    router = Router("sticky_headroom", RoutingWeights())
    first = router.select(runtimes(config), ctx()).chosen
    second = router.select(runtimes(config), ctx()).chosen
    assert first.id == second.id


def test_different_sessions_spread_across_the_pool():
    config = make_config(["a", "b", "c"])
    router = Router("sticky_headroom", RoutingWeights())
    chosen = {
        router.select(runtimes(config), ctx(session_key=f"fp:{i}")).chosen.id
        for i in range(60)
    }
    assert len(chosen) > 1


def test_affinity_beats_a_marginal_headroom_advantage():
    config = make_config(["a", "b"])
    accounts = runtimes(config)
    now = time.time()
    # b has slightly more headroom, a owns the session's cache.
    accounts[0].mirror.update_from_headers(
        {"anthropic-ratelimit-input-tokens-limit": "100000",
         "anthropic-ratelimit-input-tokens-remaining": "60000"}, now
    )
    accounts[1].mirror.update_from_headers(
        {"anthropic-ratelimit-input-tokens-limit": "100000",
         "anthropic-ratelimit-input-tokens-remaining": "90000"}, now
    )
    router = Router("sticky_headroom", RoutingWeights())
    decision = router.select(accounts, ctx(sticky_owner="a", now=now))
    assert decision.chosen.id == "a"
    assert decision.affinity_honored


def test_headroom_strategy_ignores_affinity():
    config = make_config(["a", "b"])
    accounts = runtimes(config)
    now = time.time()
    accounts[0].mirror.update_from_headers(
        {"anthropic-ratelimit-input-tokens-limit": "100000",
         "anthropic-ratelimit-input-tokens-remaining": "10000"}, now
    )
    accounts[1].mirror.update_from_headers(
        {"anthropic-ratelimit-input-tokens-limit": "100000",
         "anthropic-ratelimit-input-tokens-remaining": "99000"}, now
    )
    router = Router("headroom", RoutingWeights())
    decision = router.select(accounts, ctx(sticky_owner="a", now=now))
    assert decision.chosen.id == "b"
    assert decision.affinity_broken


def test_cost_tiered_drains_the_cheap_account_first():
    config = make_config(
        ["cheap", "dear"], account_overrides={"dear": {"cost_tier": 5.0}}
    )
    router = Router("cost_tiered", RoutingWeights())
    assert router.select(runtimes(config), ctx()).chosen.id == "cheap"


def test_priority_strategy_prefers_the_primary():
    config = make_config(
        ["backup", "primary"], account_overrides={"primary": {"priority": 1.0}}
    )
    router = Router("priority", RoutingWeights())
    assert router.select(runtimes(config), ctx()).chosen.id == "primary"


def test_round_robin_rotates():
    config = make_config(["a", "b", "c"])
    accounts = runtimes(config)
    router = Router("round_robin", RoutingWeights())
    picks = [router.select(accounts, ctx()).chosen.id for _ in range(6)]
    assert len(set(picks)) == 3


def test_excluded_accounts_are_filtered_with_a_reason():
    config = make_config(["a", "b"])
    router = Router("sticky_headroom", RoutingWeights())
    decision = router.select(runtimes(config), ctx(exclude=["a"]))
    assert decision.chosen.id == "b"
    verdicts = {c.account_id: c.verdict for c in decision.candidates}
    assert "already attempted" in verdicts["a"]


def test_key_pool_scoping():
    config = make_config(["a", "b"])
    router = Router("sticky_headroom", RoutingWeights())
    decision = router.select(runtimes(config), ctx(pool=["b"]))
    assert decision.chosen.id == "b"
    verdicts = {c.account_id: c.verdict for c in decision.candidates}
    assert "outside this key's pool" in verdicts["a"]


def test_cooling_account_reports_when_it_returns():
    config = make_config(["a"])
    accounts = runtimes(config)
    now = time.time()
    accounts[0].cooling_until = now + 42
    router = Router("sticky_headroom", RoutingWeights())
    decision = router.select(accounts, ctx(now=now))
    assert decision.chosen is None
    assert decision.retryable_later
    assert 41 <= decision.soonest_available <= 42
    assert "cooling" in decision.candidates[0].verdict


def test_request_bigger_than_any_limit_is_unservable():
    config = make_config(["a"])
    accounts = runtimes(config)
    now = time.time()
    accounts[0].mirror.update_from_headers(
        {"anthropic-ratelimit-input-tokens-limit": "1000",
         "anthropic-ratelimit-input-tokens-remaining": "1000"}, now
    )
    router = Router("sticky_headroom", RoutingWeights())
    decision = router.select(accounts, ctx(estimate=TokenEstimate(50_000, 10), now=now))
    assert decision.chosen is None
    # Nothing to wait for: no reset can make this servable.
    assert not decision.retryable_later
    assert "exceeds account limit" in decision.candidates[0].verdict


def test_every_candidate_carries_terms_for_the_inspector():
    config = make_config(["a", "b"])
    router = Router("sticky_headroom", RoutingWeights())
    decision = router.select(runtimes(config), ctx())
    payload = decision.to_dict()
    assert payload["chosen"]
    assert len(payload["candidates"]) == 2
    chosen = [c for c in payload["candidates"] if c["verdict"] == "chosen"][0]
    assert set(chosen["terms"]) == {"affinity", "headroom", "priority", "load", "errors", "cost"}
