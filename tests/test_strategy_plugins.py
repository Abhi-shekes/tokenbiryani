"""Third-party routing strategies, loaded from entry points."""

from __future__ import annotations

import time

import pytest
from conftest import make_config

from tokenbiryani.config import RoutingWeights
from tokenbiryani.core import router as router_module
from tokenbiryani.core.account import AccountRuntime
from tokenbiryani.core.limits import TokenEstimate
from tokenbiryani.core.router import (
    PLUGIN_GROUP,
    Router,
    RoutingContext,
    StrategySpec,
    available_strategies,
    load_plugin_strategies,
    register_strategy,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """Registering strategies mutates module state; put it back."""
    builtins = dict(router_module.STRATEGIES)
    custom = dict(router_module.CUSTOM_STRATEGIES)
    loaded = router_module._plugins_loaded
    yield
    router_module.STRATEGIES.clear()
    router_module.STRATEGIES.update(builtins)
    router_module.CUSTOM_STRATEGIES.clear()
    router_module.CUSTOM_STRATEGIES.update(custom)
    router_module._plugins_loaded = loaded


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


class AlwaysLast:
    """A scorer that computes its own terms: prefer the last account in the pool."""

    def terms(self, account, context, weights, index, count):
        return {"position": float(index)}


def test_a_spec_plugin_reweights_the_built_in_terms():
    register_strategy("headroom_heavy", StrategySpec(affinity=0.0, headroom=5.0))
    assert "headroom_heavy" in available_strategies()
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
    router = Router("headroom_heavy", RoutingWeights())
    assert router.select(accounts, ctx(sticky_owner="a", now=now)).chosen.id == "b"


def test_a_scorer_plugin_replaces_scoring_entirely():
    register_strategy("always_last", AlwaysLast())
    config = make_config(["a", "b", "c"])
    router = Router("always_last", RoutingWeights())
    decision = router.select(runtimes(config), ctx())
    assert decision.chosen.id == "c"


def test_plugin_terms_reach_the_request_inspector():
    register_strategy("always_last", AlwaysLast())
    config = make_config(["a", "b"])
    router = Router("always_last", RoutingWeights())
    payload = router.select(runtimes(config), ctx()).to_dict()
    chosen = [c for c in payload["candidates"] if c["verdict"] == "chosen"][0]
    assert set(chosen["terms"]) == {"position"}


def test_registering_rubbish_is_refused():
    with pytest.raises(TypeError, match="terms"):
        register_strategy("nope", object())


def test_unknown_strategy_names_the_known_ones():
    with pytest.raises(ValueError) as excinfo:
        Router("does_not_exist", RoutingWeights())
    assert "sticky_headroom" in str(excinfo.value)


def test_entry_points_are_loaded(monkeypatch):
    class FakeEntryPoint:
        name = "from_plugin"
        group = PLUGIN_GROUP

        def load(self):
            return StrategySpec(cost=9.0)

    monkeypatch.setattr(
        router_module,
        "_entry_points",
        lambda group: [FakeEntryPoint()] if group == PLUGIN_GROUP else [],
    )
    assert load_plugin_strategies(force=True) == ["from_plugin"]
    assert "from_plugin" in available_strategies()
    assert Router("from_plugin", RoutingWeights()).spec.cost == 9.0


def test_a_broken_plugin_is_skipped_not_fatal(monkeypatch):
    class Exploding:
        name = "boom"
        group = PLUGIN_GROUP

        def load(self):
            raise ImportError("no such module")

    class Fine:
        name = "fine"
        group = PLUGIN_GROUP

        def load(self):
            return StrategySpec()

    monkeypatch.setattr(router_module, "_entry_points", lambda group: [Exploding(), Fine()])
    loaded = load_plugin_strategies(force=True)
    assert loaded == ["fine"], "a broken plugin must not stop the good ones loading"
    assert "boom" not in available_strategies()


def test_gateway_accepts_a_plugin_strategy(gateway_factory, mock, key):
    register_strategy("always_last", AlwaysLast())
    gateway = gateway_factory(["a", "b"], strategy="always_last")
    assert gateway.router.strategy == "always_last"
