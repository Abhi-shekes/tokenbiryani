"""Per-conversation budgets: what catches one agent loop inside a tenant's allowance."""

from __future__ import annotations

import pytest
from conftest import body

from tokenbiryani.config import KeyConfig
from tokenbiryani.core.gateway import GatewayError

PRICED = {"claude-test-1": {"input": 3.0, "output": 15.0}}


def capped(**caps) -> KeyConfig:
    return KeyConfig(key="bir_test", name="default", **caps)


def convo(turn: int):
    """One conversation, growing.

    The opening exchange is fixed, because the fingerprint covers the system prompt
    and the first *two* messages: a helper whose turn 0 has only one message would
    put that turn on a different session from the rest, which is correct behaviour
    and a broken fixture.
    """
    messages = [
        {"role": "user", "content": "start the task"},
        {"role": "assistant", "content": "starting"},
    ]
    for index in range(turn):
        messages.append({"role": "user", "content": f"continue {index}"})
        messages.append({"role": "assistant", "content": f"step {index}"})
    return body(system="a stable system prompt", messages=messages)


# ---- turn caps -------------------------------------------------------------------


async def test_a_conversation_is_stopped_at_its_turn_limit(gateway_factory):
    gateway = gateway_factory(["acct-01"])
    key = capped(session_max_turns=5)

    for turn in range(5):
        await gateway.complete(convo(turn), {}, key)

    with pytest.raises(GatewayError) as raised:
        await gateway.complete(convo(5), {}, key)
    assert raised.value.status == 429
    assert "5 turns" in str(raised.value)


async def test_a_different_conversation_is_unaffected(gateway_factory):
    """The cap is per conversation, not per key — that is the whole point."""
    gateway = gateway_factory(["acct-01"])
    key = capped(session_max_turns=3)

    for turn in range(3):
        await gateway.complete(convo(turn), {}, key)

    other = body(system="an entirely different system prompt")
    await gateway.complete(other, {}, key)  # must not raise


async def test_turns_are_counted_even_when_the_model_has_no_price(gateway_factory):
    """A runaway loop on an unpriced model is still a runaway loop, and a turn cap
    that only worked for priced models would fail where cost is unknown."""
    gateway = gateway_factory(["acct-01"])
    key = capped(session_max_turns=2)

    for turn in range(2):
        await gateway.complete(convo(turn), {}, key)
    with pytest.raises(GatewayError):
        await gateway.complete(convo(2), {}, key)


# ---- spend caps ------------------------------------------------------------------


async def test_a_conversation_is_stopped_at_its_spend_limit(gateway_factory):
    gateway = gateway_factory(["acct-01"], overrides={"pricing": PRICED})
    key = capped(session_cap_usd=0.0000001)

    await gateway.complete(convo(0), {}, key)
    with pytest.raises(GatewayError) as raised:
        await gateway.complete(convo(1), {}, key)
    assert raised.value.status == 429
    assert "this conversation has spent" in str(raised.value)


async def test_a_generous_cap_lets_a_conversation_run(gateway_factory):
    gateway = gateway_factory(["acct-01"], overrides={"pricing": PRICED})
    key = capped(session_cap_usd=1000.0)
    for turn in range(6):
        await gateway.complete(convo(turn), {}, key)


async def test_no_cap_means_no_check(gateway_factory, key):
    gateway = gateway_factory(["acct-01"], overrides={"pricing": PRICED})
    for turn in range(6):
        await gateway.complete(convo(turn), {}, key)


# ---- the runaway report ----------------------------------------------------------


async def test_the_report_ranks_conversations_by_cost(gateway_factory, key):
    gateway = gateway_factory(["acct-01"], overrides={"pricing": PRICED})

    for turn in range(6):
        await gateway.complete(convo(turn), {}, key)
    await gateway.complete(body(system="a quiet little conversation"), {}, key)

    report = await gateway.sessions_report()
    assert report["tracking"] is True
    assert len(report["sessions"]) == 2
    assert report["sessions"][0]["turns"] == 6
    assert report["sessions"][0]["spend_usd"] > report["sessions"][1]["spend_usd"]


async def test_a_runaway_is_flagged_with_a_reason(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"],
        overrides={"pricing": PRICED, "sessions": {"runaway_turns": 3}},
    )
    for turn in range(4):
        await gateway.complete(convo(turn), {}, key)

    worst = (await gateway.sessions_report())["sessions"][0]
    assert worst["runaway"] is True
    assert "4 turns" in worst["why"]


async def test_an_ordinary_conversation_is_not_flagged(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"],
        overrides={"pricing": PRICED, "sessions": {"runaway_turns": 100}},
    )
    for turn in range(3):
        await gateway.complete(convo(turn), {}, key)
    assert (await gateway.sessions_report())["sessions"][0]["runaway"] is False


async def test_the_report_is_bounded(gateway_factory, key):
    """Sessions are unbounded in number; the report is the tail that matters."""
    gateway = gateway_factory(
        ["acct-01"],
        overrides={"pricing": PRICED, "sessions": {"report_limit": 3}},
    )
    for index in range(8):
        await gateway.complete(body(system=f"conversation number {index}"), {}, key)
    assert len((await gateway.sessions_report())["sessions"]) == 3


# ---- tracking off ----------------------------------------------------------------


async def test_tracking_off_goes_quiet_rather_than_lying(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"], overrides={"pricing": PRICED, "sessions": {"track": False}}
    )
    for turn in range(4):
        await gateway.complete(convo(turn), {}, key)

    report = await gateway.sessions_report()
    assert report["tracking"] is False
    assert report["sessions"] == []


async def test_caps_are_not_enforced_when_tracking_is_off(gateway_factory):
    """Stated so it is a decision rather than a surprise: the counters a cap reads
    are the ones tracking writes, so turning tracking off turns the caps off."""
    gateway = gateway_factory(["acct-01"], overrides={"sessions": {"track": False}})
    key = capped(session_max_turns=2)
    for turn in range(5):
        await gateway.complete(convo(turn), {}, key)


# ---- minted keys -----------------------------------------------------------------


async def test_a_minted_key_carries_its_session_caps(gateway_factory):
    gateway = gateway_factory(["acct-01"])
    _, record = await gateway.create_key(
        "tenant-1", session_cap_usd=5.0, session_max_turns=50
    )
    assert record["session_cap_usd"] == 5.0
    assert record["session_max_turns"] == 50

    restored = gateway.keys.by_name("tenant-1")
    assert restored.session_cap_usd == 5.0
    assert restored.session_max_turns == 50
