"""The commit boundary — the one failure the gateway cannot hide."""

from __future__ import annotations

from conftest import body, drain
from support.mock_upstream import (
    invalid_request,
    ok,
    rate_limit,
    stream_disconnect,
)


async def test_stream_passes_through_untouched(gateway_factory, mock, key):
    gateway = gateway_factory(["a"])
    headers, iterator = await gateway.stream(body(stream=True), {}, key)
    assert headers["content-type"] == "text/event-stream"
    output = await drain(iterator)
    assert b"message_start" in output
    assert b"content_block_delta" in output
    assert b"message_stop" in output
    assert b"event: error" not in output


async def test_failure_before_first_token_is_retried_invisibly(gateway_factory, mock, key):
    # `priority` fixes the first pick so exactly one account dies mid-handshake.
    gateway = gateway_factory(
        ["a", "b"], strategy="priority", account_overrides={"a": {"priority": 1.0}}
    )
    mock.script("a", stream_disconnect(after_chunks=0))
    _, iterator = await gateway.stream(body(stream=True), {}, key)
    output = await drain(iterator)

    assert b"event: error" not in output, "a pre-commit failure must stay invisible"
    # Count frames, not the substring: "message_start" appears twice inside one frame.
    assert output.count(b"event: message_start") == 1, "the dead attempt's bytes must be discarded"
    assert b"content_block_delta" in output


async def test_429_before_streaming_fails_over(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["a", "b"], strategy="priority", account_overrides={"a": {"priority": 1.0}}
    )
    mock.script("a", rate_limit(30))
    _, iterator = await gateway.stream(body(stream=True), {}, key)
    output = await drain(iterator)
    assert b"event: error" not in output
    assert b"content_block_delta" in output


async def test_failure_after_first_token_surfaces_as_an_sse_error(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b"])
    for name in ("a", "b"):
        mock.script(name, stream_disconnect(after_chunks=3))
    _, iterator = await gateway.stream(body(stream=True), {}, key)
    output = await drain(iterator)

    assert b"content_block_delta" in output, "the client already had content"
    assert b"event: error" in output, "and must be told the stream died"
    assert len(mock.calls) == 1, "a committed stream must never be retried"


async def test_400_on_a_stream_is_not_retried(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b"])
    for name in ("a", "b"):
        mock.script(name, invalid_request())
    _, iterator = await gateway.stream(body(stream=True), {}, key)
    output = await drain(iterator)
    assert b"event: error" in output
    assert len(mock.calls) == 1


async def test_usage_is_collected_from_the_stream(gateway_factory, mock, key):
    gateway = gateway_factory(["a"])
    mock.script("a", ok(input_tokens=321, output_tokens=45, cache_read_tokens=1000))
    _, iterator = await gateway.stream(body(stream=True), {}, key)
    await drain(iterator)
    recent = gateway.events.recent(1)[0]
    assert recent["input_tokens"] == 321
    assert recent["output_tokens"] == 45
    assert recent["cache_read_tokens"] == 1000
    assert recent["ttft"] is not None


async def test_streamed_requests_keep_affinity(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b", "c"])
    _, iterator = await gateway.stream(body("conv", stream=True), {}, key)
    await drain(iterator)
    first = gateway.events.recent(1)[0]["account_id"]
    for _ in range(3):
        _, iterator = await gateway.stream(body("conv", stream=True), {}, key)
        await drain(iterator)
        assert gateway.events.recent(1)[0]["account_id"] == first


async def test_leases_released_after_streaming(gateway_factory, mock, key):
    gateway = gateway_factory(["a"])
    _, iterator = await gateway.stream(body(stream=True), {}, key)
    await drain(iterator)
    account = gateway.accounts["a"]
    assert account.inflight == 0
    assert account.mirror.reserved_input == 0
