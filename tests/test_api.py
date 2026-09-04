"""The HTTP surface, exercised through ASGI."""

from __future__ import annotations

import json

import httpx
from conftest import body, build, make_config

from tokenbiryani.api.app import create_app
from tokenbiryani.testing.mock_upstream import invalid_request, rate_limit


def app_client(mock, account_ids=("a", "b"), **kwargs):
    config = make_config(list(account_ids), **kwargs)
    gateway = build(mock, config)
    app = create_app(config, gateway)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://gateway"), gateway


AUTH = {"x-api-key": "bir_test"}


async def test_messages_endpoint(mock):
    client, _ = app_client(mock)
    async with client:
        response = await client.post("/v1/messages", json=body(), headers=AUTH)
    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "ok"
    assert "x-tokenbiryani-account" in response.headers


async def test_missing_credentials_is_401(mock):
    client, _ = app_client(mock)
    async with client:
        response = await client.post("/v1/messages", json=body())
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


async def test_bearer_token_is_accepted(mock):
    client, _ = app_client(mock)
    async with client:
        response = await client.post(
            "/v1/messages", json=body(), headers={"authorization": "Bearer bir_test"}
        )
    assert response.status_code == 200


async def test_streaming_endpoint(mock):
    client, _ = app_client(mock)
    async with client:
        async with client.stream(
            "POST", "/v1/messages", json=body(stream=True), headers=AUTH
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            chunks = [chunk async for chunk in response.aiter_bytes()]
    output = b"".join(chunks)
    assert b"content_block_delta" in output


async def test_upstream_400_is_passed_through(mock):
    client, _ = app_client(mock)
    mock.script("a", invalid_request("bad"))
    mock.script("b", invalid_request("bad"))
    async with client:
        response = await client.post("/v1/messages", json=body(), headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_malformed_json_is_rejected(mock):
    client, _ = app_client(mock)
    async with client:
        response = await client.post(
            "/v1/messages",
            content=b"{not json",
            headers=dict(AUTH, **{"content-type": "application/json"}),
        )
    assert response.status_code == 400


async def test_healthz(mock):
    client, _ = app_client(mock)
    async with client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ready"] == 2


async def test_metrics_endpoint(mock):
    client, _ = app_client(mock)
    async with client:
        await client.post("/v1/messages", json=body(), headers=AUTH)
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert "tokenbiryani_requests_total" in response.text
    assert "tokenbiryani_account_headroom" in response.text


async def test_admin_status_requires_auth(mock):
    client, _ = app_client(mock)
    async with client:
        assert (await client.get("/admin/status")).status_code == 401
        response = await client.get("/admin/status", headers=AUTH)
    assert response.status_code == 200
    payload = response.json()
    assert payload["pool"]["total"] == 2
    assert payload["strategy"] == "sticky_headroom"
    assert len(payload["accounts"]) == 2


async def test_request_inspector_shows_the_decision(mock):
    client, _ = app_client(mock)
    mock.script("a", rate_limit(20))
    mock.script("b", rate_limit(20))
    async with client:
        await client.post("/v1/messages", json=body(), headers=AUTH)
        listing = await client.get("/admin/requests", headers=AUTH)
        request_id = listing.json()["requests"][0]["request_id"]
        detail = await client.get("/admin/requests/" + request_id, headers=AUTH)
    payload = detail.json()
    assert len(payload["attempts"]) == 2
    assert payload["decision"]["candidates"]
    assert payload["decision"]["chosen"]


async def test_horizon_endpoint(mock):
    client, _ = app_client(mock)
    async with client:
        await client.post("/v1/messages", json=body(), headers=AUTH)
        response = await client.get("/admin/horizon", headers=AUTH)
    payload = response.json()
    assert len(payload["series"]) == 12
    assert all("input_tokens" in point for point in payload["series"])


async def test_keys_are_redacted(mock):
    client, _ = app_client(mock)
    async with client:
        response = await client.get("/admin/keys", headers=AUTH)
    listed = response.json()["keys"][0]
    # bir_test is short enough that revealing a prefix would reveal the whole key.
    assert "bir_test" not in json.dumps(listed)
    assert set(listed["key"]) == {"\u2022"}
