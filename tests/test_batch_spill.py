"""The spill lane: batch-priority work goes to the Batches API instead of waiting."""

from __future__ import annotations

import json
import time

import pytest
from conftest import body

from tokenbiryani.core.gateway import GatewayError
from tokenbiryani.core.queue import PRIORITY_HEADER

BATCH = {PRIORITY_HEADER: "batch"}
SPILL_ON = {"batch": {"enabled": True, "poll_interval_seconds": 0.01}}


def saturate(gateway, seconds=600):
    for account in gateway.accounts.values():
        account.cooling_until = time.time() + seconds


async def test_batch_priority_spills_when_the_pool_is_saturated(gateway_factory, mock, key):
    gateway = gateway_factory(["a"], overrides=SPILL_ON)
    saturate(gateway)
    completion = await gateway.complete(body(), BATCH, key)

    assert completion.status == 200
    assert completion.headers["x-tokenbiryani-via"] == "batch"
    assert completion.headers["x-tokenbiryani-batch-id"].startswith("msgbatch_")
    assert json.loads(completion.content)["content"][0]["text"] == "batched"
    assert completion.event.via == "batch"


async def test_interactive_traffic_never_spills(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["a"], overrides={"batch": SPILL_ON["batch"], "queue": {"default_max_wait_seconds": 0.2}}
    )
    saturate(gateway)
    with pytest.raises(GatewayError) as excinfo:
        await gateway.complete(body(), {}, key)
    assert excinfo.value.status == 429
    assert not mock.batches, "an interactive request must wait, not spill"


async def test_spill_is_off_by_default(gateway_factory, mock, key):
    gateway = gateway_factory(["a"], overrides={"queue": {"default_max_wait_seconds": 0.2}})
    saturate(gateway)
    with pytest.raises(GatewayError):
        await gateway.complete(body(), BATCH, key)
    assert not mock.batches


async def test_a_healthy_pool_does_not_spill(gateway_factory, mock, key):
    gateway = gateway_factory(["a"], overrides=SPILL_ON)
    completion = await gateway.complete(body(), BATCH, key)
    assert completion.headers.get("x-tokenbiryani-via") is None
    assert not mock.batches, "spill is for saturation, not for batch traffic generally"


async def test_streaming_never_spills(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["a"], overrides={"batch": SPILL_ON["batch"], "queue": {"default_max_wait_seconds": 0.2}}
    )
    saturate(gateway)
    _, iterator = await gateway.stream(body(stream=True), BATCH, key)
    chunks = [chunk async for chunk in iterator]
    assert b"event: error" in b"".join(chunks)
    assert not mock.batches, "batches do not stream"


async def test_slow_batch_is_cancelled_at_the_wait_budget(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["a"],
        overrides={
            "batch": {"enabled": True, "poll_interval_seconds": 0.01},
            "queue": {"default_max_wait_seconds": 0.15},
        },
    )
    mock.accounts["a"].batch_polls_before_ready = 10_000
    saturate(gateway)

    with pytest.raises(GatewayError) as excinfo:
        await gateway.complete(body(), BATCH, key)

    assert excinfo.value.status == 429
    batch_id = excinfo.value.headers()["x-tokenbiryani-batch-id"]
    assert mock.batches[batch_id]["cancelled"], "an abandoned batch must be cancelled upstream"


async def test_a_refused_submission_falls_back_to_the_queue(gateway_factory, mock, key):
    """The spill lane is an optimisation, never a dependency."""
    gateway = gateway_factory(["a"], overrides=SPILL_ON)
    mock.accounts["a"].batch_submit_status = 500
    gateway.accounts["a"].cooling_until = time.time() + 0.15

    completion = await gateway.complete(body(), BATCH, key)

    assert completion.status == 200
    assert completion.event.via == "messages"
    kinds = [attempt.kind for attempt in completion.event.attempts]
    assert "batch_unavailable" in kinds


async def test_spilled_work_is_billed_at_the_batch_rate(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["a"],
        overrides={
            "batch": {"enabled": True, "poll_interval_seconds": 0.01, "cost_multiplier": 0.5},
            "pricing": {"claude-test-1": {"input": 10.0, "output": 40.0}},
        },
    )
    saturate(gateway)
    completion = await gateway.complete(body(), BATCH, key)

    # mock returns input_tokens=100, output_tokens=20 -> full price would be
    # (100*10 + 20*40)/1e6 = 0.0018; the batch multiplier halves it.
    assert completion.event.cost_usd == pytest.approx(0.0009)


async def test_spill_prefers_the_cache_owner(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b"], overrides=SPILL_ON)
    first = await gateway.complete(body("conv"), {}, key)
    owner = first.event.account_id
    saturate(gateway)

    completion = await gateway.complete(body("conv"), BATCH, key)
    assert completion.headers["x-tokenbiryani-account"] == owner


async def test_spills_are_counted(gateway_factory, mock, key):
    gateway = gateway_factory(["a"], overrides=SPILL_ON)
    saturate(gateway)
    await gateway.complete(body(), BATCH, key)
    assert "tokenbiryani_batch_spills_total" in gateway.metrics.render()
