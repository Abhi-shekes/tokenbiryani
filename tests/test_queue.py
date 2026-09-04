"""Admission control and backpressure."""

from __future__ import annotations

import asyncio

import pytest

from tokenbiryani.core.queue import (
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    CapacityGate,
    QueueFull,
    admission_check,
)


async def test_waiter_times_out_rather_than_hanging():
    gate = CapacityGate()
    assert await gate.wait(PRIORITY_INTERACTIVE, timeout=0.05) is False
    assert gate.depth == 0


async def test_wake_one_releases_a_waiter():
    gate = CapacityGate()
    task = asyncio.ensure_future(gate.wait(PRIORITY_INTERACTIVE, timeout=5.0))
    await asyncio.sleep(0.02)
    assert gate.depth == 1
    await gate.wake_one()
    assert await task is True


async def test_interactive_traffic_is_woken_before_batch():
    gate = CapacityGate()
    batch = asyncio.ensure_future(gate.wait(PRIORITY_BATCH, timeout=5.0))
    await asyncio.sleep(0.02)
    interactive = asyncio.ensure_future(gate.wait(PRIORITY_INTERACTIVE, timeout=5.0))
    await asyncio.sleep(0.02)
    await gate.wake_one()
    assert await asyncio.wait_for(interactive, timeout=1.0) is True
    batch.cancel()
    await asyncio.gather(batch, return_exceptions=True)


async def test_full_queue_sheds_load():
    gate = CapacityGate(max_size=1)
    first = asyncio.ensure_future(gate.wait(PRIORITY_INTERACTIVE, timeout=5.0))
    await asyncio.sleep(0.02)
    with pytest.raises(QueueFull):
        await gate.wait(PRIORITY_INTERACTIVE, timeout=1.0)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)


def test_admission_fails_fast_when_no_reset_would_help():
    result = admission_check(False, None)
    assert not result.admitted
    assert result.retry_after is None
    assert "no account" in result.reason


def test_admission_reports_a_real_retry_after():
    result = admission_check(False, 42.0)
    assert not result.admitted
    assert result.retry_after == 42.0
