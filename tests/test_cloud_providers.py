"""Bedrock and Vertex: the two upstreams that need translation, and only there."""

from __future__ import annotations

import base64
import binascii
import json
import struct

import httpx
import pytest

from tokenbiryani.config import AccountConfig
from tokenbiryani.providers.anthropic_api import build_upstream
from tokenbiryani.providers.bedrock import BedrockSigner, BedrockUpstream
from tokenbiryani.providers.translate import (
    BEDROCK_VERSION,
    VERTEX_VERSION,
    map_model,
    to_platform_body,
)
from tokenbiryani.providers.vertex import VertexUpstream

BODY = {
    "model": "claude-test-1",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hello"}],
}


# ---- the translation itself ----------------------------------------------------

def test_model_moves_out_of_the_body_and_the_version_moves_in():
    translated = to_platform_body(BODY, BEDROCK_VERSION)
    assert "model" not in translated, "the model is addressed in the URL"
    assert translated["anthropic_version"] == BEDROCK_VERSION
    assert translated["messages"] == BODY["messages"], "everything else is untouched"
    assert translated["max_tokens"] == 64


def test_unknown_fields_survive_translation():
    """A beta parameter we have never heard of must still reach the platform."""
    translated = to_platform_body(dict(BODY, some_future_flag={"a": 1}), VERTEX_VERSION)
    assert translated["some_future_flag"] == {"a": 1}


def test_model_mapping_is_exact_then_prefix_then_passthrough():
    mapping = {"claude-test-1": "platform-id-1", "claude-haiku-*": "platform-haiku"}
    assert map_model("claude-test-1", mapping) == "platform-id-1"
    assert map_model("claude-haiku-4-5", mapping) == "platform-haiku"
    assert map_model("already.a.platform.id", mapping) == "already.a.platform.id"
    assert map_model("anything", {}) == "anything"


# ---- Vertex --------------------------------------------------------------------

def vertex_account(**options):
    settings = {"project": "my-project", "region": "us-central1"}
    settings.update(options)
    return AccountConfig(id="acct-vertex", type="vertex", options=settings)


class StubCredentials:
    def token(self):
        return "ya29.stub-token"


def test_vertex_addresses_the_model_in_the_url():
    upstream = VertexUpstream(vertex_account(model_map={"claude-test-1": "claude-3-5-v2"}),
                              credentials=StubCredentials())
    assert upstream.model_url("claude-test-1", stream=False) == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/my-project"
        "/locations/us-central1/publishers/anthropic/models/claude-3-5-v2:rawPredict"
    )
    assert upstream.model_url("claude-test-1", stream=True).endswith(":streamRawPredict")


def test_vertex_requires_a_project():
    with pytest.raises(ValueError, match="options.project"):
        VertexUpstream(AccountConfig(id="v", type="vertex", options={}))


async def test_vertex_sends_a_bearer_token_and_a_translated_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "msg", "usage": {"input_tokens": 1}})

    upstream = VertexUpstream(vertex_account(), credentials=StubCredentials())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await upstream.send(
            client, "/v1/messages", BODY, {"anthropic-version": "2023-06-01"}
        )

    assert result.status == 200
    assert seen["headers"]["authorization"] == "Bearer ya29.stub-token"
    assert "anthropic-version" not in seen["headers"], "that header means nothing to Vertex"
    assert seen["body"]["anthropic_version"] == VERTEX_VERSION
    assert "model" not in seen["body"]
    assert seen["url"].endswith(":rawPredict")


async def test_vertex_streams_native_sse():
    """Vertex returns real SSE, so iter_sse stays a passthrough."""
    frames = b"event: content_block_delta\ndata: {}\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith(":streamRawPredict")
        return httpx.Response(200, content=frames)

    upstream = VertexUpstream(vertex_account(), credentials=StubCredentials())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with upstream.open_stream(client, "/v1/messages", dict(BODY, stream=True), {}) as r:
            collected = b"".join([chunk async for chunk in upstream.iter_sse(r)])
    assert collected == frames


# ---- Bedrock -------------------------------------------------------------------

def bedrock_signer():
    from botocore.credentials import ReadOnlyCredentials

    return BedrockSigner(
        "us-east-1", credentials=ReadOnlyCredentials("AKIATEST", "secret", None)
    )


def bedrock_account(**options):
    settings = {"region": "us-east-1"}
    settings.update(options)
    return AccountConfig(id="acct-bedrock", type="bedrock", options=settings)


def test_bedrock_addresses_the_model_in_the_url():
    upstream = BedrockUpstream(
        bedrock_account(model_map={"claude-test-1": "anthropic.claude-3-5-sonnet-v2:0"}),
        signer=bedrock_signer(),
    )
    assert upstream.model_url("claude-test-1", stream=False) == (
        "https://bedrock-runtime.us-east-1.amazonaws.com"
        "/model/anthropic.claude-3-5-sonnet-v2:0/invoke"
    )
    assert upstream.model_url("claude-test-1", stream=True).endswith(
        "/invoke-with-response-stream"
    )


async def test_bedrock_signs_the_request_with_sigv4():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "msg", "usage": {"input_tokens": 1}})

    upstream = BedrockUpstream(bedrock_account(), signer=bedrock_signer())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await upstream.send(client, "/v1/messages", BODY, {})

    assert result.status == 200
    authorization = seen["headers"]["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256")
    assert "Credential=AKIATEST" in authorization
    assert "x-amz-date" in seen["headers"]
    assert seen["body"]["anthropic_version"] == BEDROCK_VERSION
    assert "model" not in seen["body"]


# ---- the AWS event-stream decoder ---------------------------------------------

def encode_frame(payload: bytes) -> bytes:
    """Build one valid AWS event-stream frame, so the decoder is really exercised."""
    headers = b""
    for name, value in ((b":message-type", b"event"), (b":event-type", b"chunk")):
        headers += struct.pack("!B", len(name)) + name
        headers += struct.pack("!B", 7) + struct.pack("!H", len(value)) + value

    total = 16 + len(headers) + len(payload)
    prelude = struct.pack("!II", total, len(headers))
    prelude += struct.pack("!I", binascii.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + headers + payload
    return message + struct.pack("!I", binascii.crc32(message) & 0xFFFFFFFF)


def bedrock_chunk(event: dict) -> bytes:
    inner = base64.b64encode(json.dumps(event).encode("utf-8")).decode("ascii")
    return encode_frame(json.dumps({"bytes": inner}).encode("utf-8"))


class StubStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


async def test_bedrock_event_stream_becomes_anthropic_sse():
    upstream = BedrockUpstream(bedrock_account(), signer=bedrock_signer())
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 12}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "message_stop"},
    ]
    response = StubStream([bedrock_chunk(event) for event in events])

    out = b"".join([chunk async for chunk in upstream.iter_sse(response)])

    assert b"event: message_start" in out
    assert b"event: content_block_delta" in out
    assert b"event: message_stop" in out
    assert b'"text": "hi"' in out
    assert out.count(b"\n\n") == 3, "one SSE frame per event"


async def test_a_frame_split_across_chunks_still_decodes():
    """TCP does not respect frame boundaries."""
    upstream = BedrockUpstream(bedrock_account(), signer=bedrock_signer())
    frame = bedrock_chunk({"type": "content_block_delta", "delta": {"text": "split"}})
    response = StubStream([frame[:9], frame[9:20], frame[20:]])

    out = b"".join([chunk async for chunk in upstream.iter_sse(response)])
    assert b"event: content_block_delta" in out
    assert b"split" in out


async def test_a_frame_with_no_payload_is_skipped():
    upstream = BedrockUpstream(bedrock_account(), signer=bedrock_signer())
    response = StubStream([encode_frame(json.dumps({}).encode("utf-8"))])
    out = b"".join([chunk async for chunk in upstream.iter_sse(response)])
    assert out == b""


# ---- dispatch ------------------------------------------------------------------

def test_build_upstream_dispatches_on_type():
    assert isinstance(
        build_upstream(AccountConfig(id="b", type="bedrock", options={"region": "us-east-1"})),
        BedrockUpstream,
    )
    assert isinstance(
        build_upstream(AccountConfig(id="v", type="vertex", options={"project": "p"})),
        VertexUpstream,
    )
    with pytest.raises(ValueError, match="known: anthropic_api, bedrock, vertex"):
        build_upstream(AccountConfig(id="x", type="azure"))


def test_base_url_is_an_override_not_a_default():
    """A cloud account must never be silently pointed at api.anthropic.com."""
    bedrock = BedrockUpstream(bedrock_account(), signer=bedrock_signer())
    assert "bedrock-runtime" in bedrock.model_url("m", stream=False)
    pinned = AccountConfig(
        id="b", type="bedrock", base_url="https://vpce.example.internal",
        options={"region": "us-east-1"},
    )
    assert BedrockUpstream(pinned, signer=bedrock_signer()).model_url("m", False).startswith(
        "https://vpce.example.internal/model/"
    )
