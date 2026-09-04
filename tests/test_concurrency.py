"""Leases under concurrency — the mechanism most likely to be subtly wrong.

Without an atomic reservation, N simultaneous requests all read the same "plenty of
headroom" and stampede one account into a 429. Nothing else in the suite fires
overlapping requests, so nothing else can catch that.
"""

from __future__ import annotations

import asyncio

from conftest import body

# ~12k characters of message, which the estimator turns into roughly 4k input tokens.
BIG = "x" * 12_000


def wide_body(tag: str = "load") -> dict:
    payload = body(tag + " " + BIG)
    payload["max_tokens"] = 256
    return payload


def watch_reservations(account):
    """Record the account's reserved input after every reserve, to find the peak."""
    original = account.mirror.reserve
    peaks = []

    def recording(estimate, account_id):
        lease = original(estimate, account_id)
        peaks.append(account.mirror.reserved_input)
        return lease

    account.mirror.reserve = recording
    return peaks


async def test_leases_never_let_reservations_exceed_the_budget(gateway_factory, mock, key):
    # The mock's accounts are created by the factory, so widen them afterwards but
    # before the priming request that puts real numbers in the mirror.
    gateway = gateway_factory(["a"], overrides={"queue": {"default_max_wait_seconds": 20}})
    upstream = mock.accounts["a"]
    upstream.input_limit = 60_000
    upstream.input_remaining = 60_000
    upstream.output_limit = 10_000_000
    upstream.output_remaining = 10_000_000

    # Prime the mirror: until a response arrives, headroom is unknown and unbounded.
    await gateway.complete(wide_body("prime"), {}, key)
    account = gateway.accounts["a"]
    budget = account.mirror.input_tokens.remaining
    assert budget is not None and budget > 0

    mock.latency = 0.05  # force real overlap
    peaks = watch_reservations(account)
    await asyncio.gather(*[gateway.complete(wide_body(f"c{i}"), {}, key) for i in range(12)])

    assert max(peaks) <= budget, (
        f"reserved {max(peaks)} against a budget of {budget}: "
        "concurrent requests overshot the account"
    )
    assert len(peaks) == 12


async def test_concurrency_is_actually_overlapping(gateway_factory, mock, key):
    """Guard the guard: if requests serialised, the test above proves nothing."""
    gateway = gateway_factory(["a"])
    mock.latency = 0.05
    account = gateway.accounts["a"]
    seen = []
    original = account.mirror.reserve

    def recording(estimate, account_id):
        lease = original(estimate, account_id)
        seen.append(account.inflight)
        return lease

    account.mirror.reserve = recording
    await asyncio.gather(*[gateway.complete(body(f"c{i}"), {}, key) for i in range(8)])
    assert max(seen) > 1, "requests never overlapped, so concurrency was not exercised"


async def test_no_leases_or_inflight_leak_under_load(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b", "c"])
    mock.latency = 0.01
    results = await asyncio.gather(
        *[gateway.complete(body(f"session-{i}"), {}, key) for i in range(40)]
    )
    assert all(r.status == 200 for r in results)
    for account in gateway.accounts.values():
        assert account.inflight == 0, f"{account.id} leaked in-flight count"
        assert account.mirror.reserved_input == 0, f"{account.id} leaked an input lease"
        assert account.mirror.reserved_output == 0, f"{account.id} leaked an output lease"
        assert account.mirror.reserved_requests == 0, f"{account.id} leaked a request lease"


async def test_concurrent_failures_also_release_their_leases(gateway_factory, mock, key):
    from tokenbiryani.core.gateway import GatewayError
    from tokenbiryani.testing.mock_upstream import server_error

    # Once both breakers trip there is genuinely nothing to wait for but their
    # cooldown; bound the wait rather than sit through it.
    gateway = gateway_factory(
        ["a", "b"],
        overrides={
            "retry": {"deadline_seconds": 1.0},
            "queue": {"default_max_wait_seconds": 0.3},
        },
    )
    for _ in range(60):
        mock.script("a", server_error())
        mock.script("b", server_error())
    mock.latency = 0.01

    outcomes = await asyncio.gather(
        *[gateway.complete(body(f"s{i}"), {}, key) for i in range(10)],
        return_exceptions=True,
    )
    assert any(isinstance(o, GatewayError) for o in outcomes)
    for account in gateway.accounts.values():
        assert account.inflight == 0
        assert account.mirror.reserved_input == 0


async def test_distinct_sessions_spread_across_the_pool(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b", "c"])
    mock.latency = 0.01
    results = await asyncio.gather(
        *[gateway.complete(body(f"session-{i}"), {}, key) for i in range(30)]
    )
    used = {r.event.account_id for r in results}
    assert used == {"a", "b", "c"}, f"load did not spread: {used}"


async def test_one_session_converges_on_one_account_under_concurrency(
    gateway_factory, mock, key
):
    """Affinity is established by the first completed turn; parallel turns follow it."""
    gateway = gateway_factory(["a", "b", "c"])
    first = await gateway.complete(body("shared"), {}, key)
    owner = first.event.account_id

    mock.latency = 0.02
    results = await asyncio.gather(
        *[gateway.complete(body("shared"), {}, key) for _ in range(10)]
    )
    assert {r.event.account_id for r in results} == {owner}
    assert all(r.event.affinity_honored for r in results)


async def test_streamed_requests_release_their_leases_under_concurrency(
    gateway_factory, mock, key
):
    from conftest import drain

    gateway = gateway_factory(["a", "b"])
    mock.latency = 0.01

    async def run(index):
        _, iterator = await gateway.stream(body(f"s{index}", stream=True), {}, key)
        return await drain(iterator)

    outputs = await asyncio.gather(*[run(i) for i in range(12)])
    assert all(b"content_block_delta" in out for out in outputs)
    for account in gateway.accounts.values():
        assert account.inflight == 0
        assert account.mirror.reserved_input == 0
