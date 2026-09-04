"""Upstreams that report no rate-limit headers must not read as infinitely available."""

from __future__ import annotations

import time

import pytest
from conftest import body

from tokenbiryani.config import AccountConfig
from tokenbiryani.core.limits import LimitMirror, TokenEstimate
from tokenbiryani.providers.anthropic_api import (
    PLUGIN_GROUP,
    available_types,
    build_upstream,
    register_upstream,
)


def blind(mock, gateway):
    """Make every mock account behave like an upstream with no limit headers."""
    for account in mock.accounts.values():
        account.emit_limit_headers = False


def test_an_unobservable_mirror_does_not_claim_to_be_full():
    now = time.time()
    observable = LimitMirror()
    unobservable = LimitMirror(observable=False, assumed_headroom=0.5)
    assert observable.headroom(now) == 1.0, "unknown-but-observable is optimistic"
    assert unobservable.headroom(now) == 0.5, "unobservable is not optimistic"


def test_an_unobservable_account_still_serves():
    """We cannot know its budget, so we cannot refuse on those grounds."""
    mirror = LimitMirror(observable=False)
    assert mirror.can_serve(TokenEstimate(1_000_000, 10_000), time.time())
    assert mirror.next_reset(time.time()) is None


def test_headers_still_win_when_an_unobservable_account_sends_them():
    mirror = LimitMirror(observable=False, assumed_headroom=0.5)
    now = time.time()
    mirror.update_from_headers(
        {"anthropic-ratelimit-input-tokens-limit": "100",
         "anthropic-ratelimit-input-tokens-remaining": "20"}, now
    )
    assert mirror.headroom(now) == pytest.approx(0.2), "real data beats the assumption"


async def test_an_unobservable_account_does_not_starve_an_observable_one(
    gateway_factory, mock, key
):
    """The bug this prevents: a blind account wins every comparison forever."""
    gateway = gateway_factory(
        ["blind", "seen"],
        strategy="headroom",
        account_overrides={"blind": {"observable_limits": False}},
    )
    mock.accounts["blind"].emit_limit_headers = False
    # `seen` reports a healthy but not-quite-full budget.
    mock.accounts["seen"].input_limit = 100_000
    mock.accounts["seen"].input_remaining = 80_000

    chosen = set()
    for index in range(12):
        completion = await gateway.complete(body(f"c{index}"), {}, key)
        chosen.add(completion.event.account_id)

    assert "seen" in chosen, "the honest account must remain reachable"


async def test_the_capacity_horizon_excludes_what_it_cannot_see(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["blind", "seen"],
        account_overrides={"blind": {"observable_limits": False}},
    )
    mock.accounts["blind"].emit_limit_headers = False
    mock.accounts["seen"].input_limit = 50_000
    mock.accounts["seen"].input_remaining = 50_000
    for index in range(4):
        await gateway.complete(body(f"c{index}"), {}, key)

    horizon = gateway.capacity_horizon()
    peak = max(p["input_tokens"] for p in horizon["series"])
    assert 0 < peak <= 50_000, (
        "the horizon must count only accounts whose future capacity is knowable"
    )


async def test_the_snapshot_says_which_accounts_are_unobservable(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["blind", "seen"], account_overrides={"blind": {"observable_limits": False}}
    )
    snapshot = {a["id"]: a for a in gateway.snapshot()["accounts"]}
    assert snapshot["blind"]["limits"]["observable"] is False
    assert snapshot["seen"]["limits"]["observable"] is True


# ---- provider adapters as plugins ---------------------------------------------

class DummyUpstream:
    def __init__(self, config):
        self.config = config
        self.account_id = config.id


def test_an_account_type_can_be_registered_at_runtime():
    try:
        register_upstream("dummy", DummyUpstream)
        assert "dummy" in available_types()
        built = build_upstream(AccountConfig(id="d", type="dummy"))
        assert isinstance(built, DummyUpstream)
    finally:
        from tokenbiryani.providers import anthropic_api

        anthropic_api._plugins.pop("dummy", None)


def test_built_in_types_cannot_be_shadowed():
    with pytest.raises(ValueError, match="built-in"):
        register_upstream("anthropic_api", DummyUpstream)


def test_an_unknown_type_names_what_is_available():
    with pytest.raises(ValueError) as excinfo:
        build_upstream(AccountConfig(id="x", type="nonesuch"))
    assert "anthropic_api" in str(excinfo.value)
    assert PLUGIN_GROUP in str(excinfo.value)


def test_a_broken_provider_plugin_is_skipped(monkeypatch):
    from tokenbiryani.providers import anthropic_api

    class Exploding:
        name = "boom"

        def load(self):
            raise ImportError("no such module")

    class Fine:
        name = "fine"

        def load(self):
            return DummyUpstream

    monkeypatch.setattr(anthropic_api, "_entry_points", lambda group: [Exploding(), Fine()])
    monkeypatch.setattr(anthropic_api, "_plugins_loaded", False)
    monkeypatch.setattr(anthropic_api, "_plugins", {})
    loaded = anthropic_api.plugin_upstreams()
    assert "fine" in loaded and "boom" not in loaded
