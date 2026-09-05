"""Cache breakpoints: did the caller ever ask for caching, and what if they did not."""

from __future__ import annotations

from tokenbiryani.core.cacheadvice import (
    CacheAdvisor,
    cacheable_prefix_tokens,
    has_cache_breakpoint,
    insert_cache_breakpoint,
)

BIG = "You are a careful software engineering assistant. " * 120
EPHEMERAL = {"type": "ephemeral"}


def body(**extra):
    payload = {
        "model": "claude-test-1",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hello"}],
    }
    payload.update(extra)
    return payload


# ---- detection -------------------------------------------------------------------


def test_an_unmarked_request_is_detected():
    assert has_cache_breakpoint(body(system=BIG)) is False


def test_a_marked_system_block_is_found():
    marked = body(system=[{"type": "text", "text": BIG, "cache_control": EPHEMERAL}])
    assert has_cache_breakpoint(marked) is True


def test_a_marked_tool_is_found():
    marked = body(tools=[{"name": "read", "cache_control": EPHEMERAL}])
    assert has_cache_breakpoint(marked) is True


def test_a_marked_message_block_is_found():
    marked = body(messages=[
        {"role": "user", "content": [
            {"type": "text", "text": "hi", "cache_control": EPHEMERAL},
        ]},
    ])
    assert has_cache_breakpoint(marked) is True


def test_detection_survives_a_string_system_prompt():
    """The common spelling, and the one that cannot carry a marker at all."""
    assert has_cache_breakpoint(body(system="plain string")) is False


def test_the_prefix_is_measured_from_system_and_tools():
    small = cacheable_prefix_tokens(body(system="short"))
    large = cacheable_prefix_tokens(body(system=BIG))
    assert large > small
    assert large > 1000


def test_messages_do_not_count_toward_the_prefix():
    """Only the stable head is cacheable; the conversation grows past it."""
    without = cacheable_prefix_tokens(body(system=BIG))
    with_history = cacheable_prefix_tokens(
        body(system=BIG, messages=[{"role": "user", "content": "x" * 5000}])
    )
    assert without == with_history


# ---- insertion -------------------------------------------------------------------


def test_a_string_system_prompt_is_promoted_and_marked():
    updated, inserted = insert_cache_breakpoint(body(system=BIG))
    assert inserted is True
    assert isinstance(updated["system"], list)
    assert updated["system"][0]["text"] == BIG
    assert updated["system"][0]["cache_control"] == EPHEMERAL


def test_the_last_tool_is_preferred_over_the_system_block():
    """Tools sit after the system prompt, so the breakpoint there covers both."""
    updated, inserted = insert_cache_breakpoint(
        body(system=BIG, tools=[{"name": "a"}, {"name": "b"}])
    )
    assert inserted is True
    assert "cache_control" not in updated["tools"][0]
    assert updated["tools"][1]["cache_control"] == EPHEMERAL


def test_an_already_marked_request_is_left_alone():
    original = body(system=[{"type": "text", "text": BIG, "cache_control": EPHEMERAL}])
    updated, inserted = insert_cache_breakpoint(original)
    assert inserted is False
    assert updated == original


def test_a_prefix_too_small_to_cache_is_left_alone():
    """Below Anthropic's minimum a breakpoint buys nothing, so adding one would be
    a rewrite with no upside — and rewriting is the thing this gateway avoids."""
    updated, inserted = insert_cache_breakpoint(body(system="tiny"))
    assert inserted is False
    assert updated["system"] == "tiny"


def test_insertion_does_not_mutate_the_caller_s_body():
    original = body(system=BIG, tools=[{"name": "a"}])
    insert_cache_breakpoint(original)
    assert "cache_control" not in original["tools"][0]


def test_nothing_to_mark_is_not_an_error():
    updated, inserted = insert_cache_breakpoint(body())
    assert inserted is False
    assert updated["messages"] == body()["messages"]


# ---- the advisor -----------------------------------------------------------------


def feed(advisor, count, marked, prefix=4000, cache_read=0, input_tokens=4000):
    for _ in range(count):
        advisor.observe("tenant-1", "claude-test-1", marked, prefix, input_tokens,
                        cache_read, 0)


def only_finding(advisor):
    findings = advisor.advice()["findings"]
    assert len(findings) == 1
    return findings[0]


def test_it_stays_quiet_until_there_is_enough_traffic():
    advisor = CacheAdvisor(min_requests=20)
    feed(advisor, 19, marked=False)
    assert advisor.advice()["findings"] == []


def test_a_missing_breakpoint_is_named_as_a_client_problem():
    advisor = CacheAdvisor(min_requests=10)
    feed(advisor, 20, marked=False)
    finding = only_finding(advisor)
    assert finding["breakpoint_rate"] == 0.0
    assert "no cache_control breakpoint" in finding["verdict"]
    assert "no routing strategy can recover it" in finding["verdict"]


def test_a_small_prefix_is_not_reported_as_a_problem():
    """Nothing to fix: it is too short for Anthropic to cache either way."""
    advisor = CacheAdvisor(min_requests=10)
    feed(advisor, 20, marked=False, prefix=100)
    assert "nothing to fix here" in only_finding(advisor)["verdict"]


def test_breakpoints_present_but_cold_is_named_as_a_routing_problem():
    advisor = CacheAdvisor(min_requests=10)
    feed(advisor, 20, marked=True, cache_read=0)
    assert "routing problem" in only_finding(advisor)["verdict"]


def test_a_working_cache_says_so():
    advisor = CacheAdvisor(min_requests=10)
    feed(advisor, 20, marked=True, cache_read=9000, input_tokens=1000)
    finding = only_finding(advisor)
    assert finding["cache_hit_rate"] > 0.8
    assert finding["verdict"] == "caching is engaged and working"


def test_keys_and_models_are_reported_separately():
    advisor = CacheAdvisor(min_requests=5)
    for _ in range(10):
        advisor.observe("tenant-1", "model-a", False, 4000, 4000, 0, 0)
        advisor.observe("tenant-2", "model-b", True, 4000, 500, 4000, 0)
    findings = advisor.advice()["findings"]
    assert {(f["key"], f["model"]) for f in findings} == {
        ("tenant-1", "model-a"), ("tenant-2", "model-b")
    }


def test_reconfiguring_keeps_the_counters():
    advisor = CacheAdvisor(min_requests=100)
    feed(advisor, 20, marked=False)
    assert advisor.advice()["findings"] == []
    advisor.reconfigure(min_requests=10)
    assert len(advisor.advice()["findings"]) == 1


# ---- through the gateway ---------------------------------------------------------


async def test_the_gateway_records_whether_a_breakpoint_was_sent(gateway_factory, key):
    gateway = gateway_factory(["acct-01"])
    for _ in range(5):
        await gateway.complete(body(system=BIG), {}, key)
    assert all(e["cache_breakpoint"] is False for e in gateway.events.recent(5))

    marked = body(system=[{"type": "text", "text": BIG, "cache_control": EPHEMERAL}])
    await gateway.complete(marked, {}, key)
    assert gateway.events.recent(1)[0]["cache_breakpoint"] is True


async def test_auto_breakpoint_is_off_by_default(gateway_factory, key):
    """The gateway routes rather than rewrites unless told otherwise."""
    gateway = gateway_factory(["acct-01"])
    await gateway.complete(body(system=BIG), {}, key)
    assert gateway.events.recent(1)[0]["cache_breakpoint"] is False


async def test_auto_breakpoint_marks_an_unmarked_request(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"], overrides={"cache": {"auto_breakpoint": True}}
    )
    await gateway.complete(body(system=BIG), {}, key)
    assert gateway.events.recent(1)[0]["cache_breakpoint"] is True


async def test_advice_reaches_an_operator(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"], overrides={"cache": {"advice_min_requests": 5}}
    )
    for _ in range(6):
        await gateway.complete(body(system=BIG), {}, key)
    findings = gateway.cache_advisor.advice()["findings"]
    assert len(findings) == 1
    assert findings[0]["key"] == "default"
    assert "no cache_control breakpoint" in findings[0]["verdict"]
