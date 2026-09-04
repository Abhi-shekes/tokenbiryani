"""End-to-end behaviour of the retry loop against the mock upstream."""

from __future__ import annotations

import json
import time

import pytest
from conftest import body

from tokenbiryani.core.account import AccountState
from tokenbiryani.core.gateway import GatewayError
from tokenbiryani.testing.mock_upstream import (
    auth_error,
    invalid_request,
    model_not_permitted,
    ok,
    overloaded,
    rate_limit,
    server_error,
    transport_error,
)


async def test_happy_path(gateway_factory, mock, key):
    gateway = gateway_factory(["a"])
    completion = await gateway.complete(body(), {}, key)
    assert completion.status == 200
    assert json.loads(completion.content)["content"][0]["text"] == "ok"
    assert completion.headers["x-tokenbiryani-account"] == "a"
    assert completion.event.account_id == "a"


async def test_429_fails_over_to_another_account(gateway_factory, mock, key):
    # `priority` makes the first pick deterministic, so exactly one account 429s.
    gateway = gateway_factory(
        ["a", "b"], strategy="priority", account_overrides={"a": {"priority": 1.0}}
    )
    mock.script("a", rate_limit(retry_after=30))
    completion = await gateway.complete(body(), {}, key)
    assert completion.status == 200
    assert len(completion.event.attempts) == 2
    assert completion.event.attempts[0].kind == "rate_limit"
    assert completion.event.attempts[1].kind == "ok"


async def test_429_puts_the_account_in_cooling(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b"])
    mock.script("a", rate_limit(retry_after=45))
    mock.script("b", rate_limit(retry_after=45))
    # Both accounts 429: the retry budget is exhausted and the caller is told so.
    with pytest.raises(GatewayError):
        await gateway.complete(body(), {}, key)
    now = time.time()
    cooling = [a for a in gateway.accounts.values() if a.state(now) is AccountState.COOLING]
    assert len(cooling) == 2
    assert all(44 <= (a.cooling_until - now) <= 45 for a in cooling)


async def test_400_is_returned_without_touching_another_account(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b"])
    for name in ("a", "b"):
        mock.script(name, invalid_request("messages: field required"))
    completion = await gateway.complete(body(), {}, key)
    assert completion.status == 400
    assert len(mock.calls) == 1, "a client error must not be amplified across the pool"
    assert len(completion.event.attempts) == 1


async def test_401_disables_the_account_and_retries_elsewhere(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["a", "b"], strategy="priority", account_overrides={"a": {"priority": 1.0}}
    )
    mock.script("a", auth_error())
    completion = await gateway.complete(body(), {}, key)
    assert completion.status == 200
    now = time.time()
    disabled = [a for a in gateway.accounts.values() if a.state(now) is AccountState.DISABLED]
    assert len(disabled) == 1
    assert disabled[0].disabled_reason == "invalid_auth"


async def test_403_marks_the_model_unsupported_only(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["a", "b"], strategy="priority", account_overrides={"a": {"priority": 1.0}}
    )
    mock.script("a", model_not_permitted())
    completion = await gateway.complete(body(), {}, key)
    assert completion.status == 200
    marked = [a for a in gateway.accounts.values() if a.unsupported_models]
    assert len(marked) == 1
    assert "claude-test-1" in marked[0].unsupported_models
    assert marked[0].state(time.time()) is AccountState.READY


async def test_529_is_retried(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["a", "b"], strategy="priority", account_overrides={"a": {"priority": 1.0}}
    )
    mock.script("a", overloaded())
    completion = await gateway.complete(body(), {}, key)
    assert completion.status == 200
    assert completion.event.attempts[0].kind == "overloaded"


async def test_transport_failure_is_retried(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["a", "b"], strategy="priority", account_overrides={"a": {"priority": 1.0}}
    )
    mock.script("a", transport_error())
    completion = await gateway.complete(body(), {}, key)
    assert completion.status == 200
    assert completion.event.attempts[0].kind == "transport"


async def test_retry_budget_stops_at_max_accounts(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b"])
    for name in ("a", "b"):
        mock.script(name, rate_limit(5), rate_limit(5))
    with pytest.raises(GatewayError) as excinfo:
        await gateway.complete(body(), {}, key)
    assert excinfo.value.status in (429, 503)


async def test_affinity_keeps_a_conversation_on_one_account(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b", "c"])
    first = await gateway.complete(body("same conversation"), {}, key)
    chosen = first.event.account_id
    for _ in range(5):
        again = await gateway.complete(body("same conversation"), {}, key)
        assert again.event.account_id == chosen
        assert again.event.affinity_honored
    assert not any(
        event["affinity_broken"] for event in gateway.events.recent(10)
    )


async def test_affinity_breaks_only_when_the_owner_cannot_serve(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b"])
    first = await gateway.complete(body("conv"), {}, key)
    owner = first.event.account_id
    gateway.accounts[owner].cooling_until = time.time() + 120

    second = await gateway.complete(body("conv"), {}, key)
    assert second.event.account_id != owner
    assert second.event.affinity_broken


async def test_cache_tokens_are_accounted(gateway_factory, mock, key):
    gateway = gateway_factory(["a"])
    mock.script("a", ok(cache_read_tokens=9_000, input_tokens=200, output_tokens=50))
    completion = await gateway.complete(body(), {}, key)
    assert completion.event.cache_read_tokens == 9_000
    assert completion.event.cache_hit
    account = gateway.accounts["a"]
    assert account.cache_hit_rate() > 0.9


async def test_limit_headers_are_mirrored(gateway_factory, mock, key):
    gateway = gateway_factory(["a"])
    mock.accounts["a"].input_limit = 10_000
    mock.accounts["a"].input_remaining = 4_000
    await gateway.complete(body(), {}, key)
    mirror = gateway.accounts["a"].mirror
    assert mirror.input_tokens.limit == 10_000
    assert mirror.input_tokens.remaining is not None
    assert mirror.headroom(time.time()) < 1.0


async def test_no_eligible_account_fails_fast_when_nothing_will_help(gateway_factory, mock, key):
    gateway = gateway_factory(["a"])
    gateway.accounts["a"].disabled_reason = "invalid_auth"
    with pytest.raises(GatewayError) as excinfo:
        await gateway.complete(body(), {}, key)
    assert excinfo.value.status == 503


async def test_key_scoped_to_a_pool_only_uses_that_pool(gateway_factory, mock, key):
    from tokenbiryani.config import KeyConfig

    gateway = gateway_factory(["a", "b"])
    scoped = KeyConfig(key="bir_test", name="scoped", pool=["b"])
    for _ in range(4):
        completion = await gateway.complete(body("x"), {}, scoped)
        assert completion.event.account_id == "b"


async def test_unknown_model_for_key_is_rejected(gateway_factory, mock, key):
    from tokenbiryani.config import KeyConfig

    gateway = gateway_factory(["a"])
    restricted = KeyConfig(key="bir_test", name="haiku-only", models=["claude-haiku-*"])
    with pytest.raises(GatewayError) as excinfo:
        await gateway.complete(body(), {}, restricted)
    assert excinfo.value.status == 403


async def test_missing_model_is_a_400(gateway_factory, mock, key):
    gateway = gateway_factory(["a"])
    with pytest.raises(GatewayError) as excinfo:
        await gateway.complete({"messages": []}, {}, key)
    assert excinfo.value.status == 400


async def test_leases_are_released_after_every_attempt(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b"])
    mock.script("a", rate_limit(5))
    mock.script("b", rate_limit(5))
    with pytest.raises(GatewayError):
        await gateway.complete(body(), {}, key)
    for account in gateway.accounts.values():
        assert account.mirror.reserved_input == 0
        assert account.mirror.reserved_requests == 0
        assert account.inflight == 0


async def test_breaker_opens_after_repeated_failures(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["a", "b"],
        overrides={
            "breaker": {"failure_threshold": 2, "cooldown_seconds": 30},
            # Once both breakers open there is nothing to wait for; don't sit on the
            # queue for the full cooldown just to prove it.
            "retry": {"deadline_seconds": 0.3},
        },
    )
    for _ in range(4):
        mock.script("a", server_error())
        mock.script("b", server_error())
    for _ in range(3):
        try:
            await gateway.complete(body(), {}, key)
        except GatewayError:
            pass
    now = time.time()
    assert any(a.breaker.state(now).value != "closed" for a in gateway.accounts.values())
