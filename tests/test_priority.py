"""Request priority and the wait budget — the queue's client-facing half."""

from __future__ import annotations

import asyncio
import time

import pytest
from conftest import body

from tokenbiryani.config import KeyConfig
from tokenbiryani.core.gateway import GatewayError
from tokenbiryani.core.queue import (
    MAX_WAIT_HEADER,
    PRIORITY_BATCH,
    PRIORITY_HEADER,
    PRIORITY_INTERACTIVE,
    parse_priority,
    priority_name,
)


def test_priority_parsing_round_trips():
    assert parse_priority("batch") == PRIORITY_BATCH
    assert parse_priority("INTERACTIVE") == PRIORITY_INTERACTIVE
    assert parse_priority(None) == PRIORITY_INTERACTIVE
    assert parse_priority("nonsense") == PRIORITY_INTERACTIVE
    assert parse_priority("nonsense", PRIORITY_BATCH) == PRIORITY_BATCH
    assert priority_name(PRIORITY_BATCH) == "batch"


async def test_header_sets_the_request_priority(gateway_factory, mock, key):
    gateway = gateway_factory(["a"])
    completion = await gateway.complete(body(), {PRIORITY_HEADER: "batch"}, key)
    assert completion.event.priority == "batch"


async def test_key_default_priority_applies(gateway_factory, mock):
    gateway = gateway_factory(["a"])
    batch_key = KeyConfig(key="bir_test", name="batch-key", priority="batch")
    completion = await gateway.complete(body(), {}, batch_key)
    assert completion.event.priority == "batch"


async def test_header_overrides_the_key_default(gateway_factory, mock):
    gateway = gateway_factory(["a"])
    batch_key = KeyConfig(key="bir_test", name="batch-key", priority="batch")
    completion = await gateway.complete(body(), {PRIORITY_HEADER: "interactive"}, batch_key)
    assert completion.event.priority == "interactive"


async def test_a_client_may_shorten_its_wait_but_not_extend_it(gateway_factory, mock, key):
    gateway = gateway_factory(["a"], overrides={"queue": {"default_max_wait_seconds": 30}})
    # Nothing to route to, so the request goes straight to the queue and gives up.
    gateway.accounts["a"].cooling_until = time.time() + 600

    started = time.time()
    with pytest.raises(GatewayError) as excinfo:
        await gateway.complete(body(), {MAX_WAIT_HEADER: "0.2"}, key)
    elapsed = time.time() - started

    assert excinfo.value.status == 429
    assert elapsed < 5, "the client's shorter budget must be honoured"
    assert excinfo.value.retry_after is not None


async def test_an_oversized_client_budget_is_capped_by_the_operator(gateway_factory, mock, key):
    gateway = gateway_factory(["a"], overrides={"queue": {"default_max_wait_seconds": 0.2}})
    gateway.accounts["a"].cooling_until = time.time() + 600
    started = time.time()
    with pytest.raises(GatewayError):
        await gateway.complete(body(), {MAX_WAIT_HEADER: "9999"}, key)
    assert time.time() - started < 5, "operator ceiling wins over the client's request"


async def test_wait_budget_is_cumulative_and_reported(gateway_factory, mock, key):
    gateway = gateway_factory(["a"], overrides={"queue": {"default_max_wait_seconds": 0.3}})
    gateway.accounts["a"].cooling_until = time.time() + 600
    with pytest.raises(GatewayError):
        await gateway.complete(body(), {}, key)
    event = gateway.events.recent(1)[0]
    assert event["status"] == 429


async def test_interactive_requests_are_served_before_batch(gateway_factory, mock, key):
    """Both wait on a saturated pool; the interactive one gets the capacity first."""
    gateway = gateway_factory(["a"], overrides={"queue": {"default_max_wait_seconds": 10}})
    account = gateway.accounts["a"]
    account.cooling_until = time.time() + 0.6

    order = []

    async def issue(label, headers):
        try:
            await gateway.complete(body(label), headers, key)
        except GatewayError:
            pass
        order.append(label)

    batch = asyncio.ensure_future(issue("batch", {PRIORITY_HEADER: "batch"}))
    await asyncio.sleep(0.05)
    interactive = asyncio.ensure_future(issue("interactive", {}))
    await asyncio.sleep(0.05)

    # Capacity returns; the gate wakes the highest-priority waiter first.
    account.cooling_until = 0
    await gateway.gate.wake_one()
    await asyncio.wait_for(asyncio.gather(interactive, batch), timeout=10)

    assert order[0] == "interactive", "batch queued first but must yield"


async def test_priority_is_recorded_on_the_request(gateway_factory, mock, key):
    """It used to be asserted through a Prometheus label. The event log is where the
    priority actually lives, and it is what the console and the usage history read."""
    gateway = gateway_factory(["a"])
    await gateway.complete(body(), {PRIORITY_HEADER: "batch"}, key)
    assert gateway.events.recent(1)[0]["priority"] == "batch"
