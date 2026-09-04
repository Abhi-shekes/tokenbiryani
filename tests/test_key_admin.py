"""Runtime key management, and keeping tenants out of /admin."""

from __future__ import annotations

import httpx
import pytest
from conftest import body, build, make_config

from tokenbiryani.config import KeyConfig
from tokenbiryani.core.gateway import GatewayError
from tokenbiryani.core.keys import hash_key

ADMIN = {"x-api-key": "bir_test"}


def app_with(mock, account_ids=("a", "b"), extra_keys=(), **kwargs):
    from tokenbiryani.api.app import create_app

    config = make_config(list(account_ids), **kwargs)
    config.keys.extend(extra_keys)
    gateway = build(mock, config)
    app = create_app(config, gateway)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
    return client, gateway


# ---- the admin boundary --------------------------------------------------------

async def test_a_tenant_key_cannot_read_the_pool(mock):
    tenant = KeyConfig(key="bir_tenant", name="tenant")
    client, _ = app_with(mock, extra_keys=[tenant])
    async with client:
        allowed = await client.get("/admin/status", headers=ADMIN)
        refused = await client.get("/admin/status", headers={"x-api-key": "bir_tenant"})
    assert allowed.status_code == 200
    assert refused.status_code == 403
    assert refused.json()["error"]["type"] == "permission_error"


async def test_a_tenant_key_still_serves_traffic(mock):
    tenant = KeyConfig(key="bir_tenant", name="tenant")
    client, _ = app_with(mock, extra_keys=[tenant])
    async with client:
        response = await client.post(
            "/v1/messages", json=body(), headers={"x-api-key": "bir_tenant"}
        )
    assert response.status_code == 200


async def test_a_tenant_key_cannot_mint_keys(mock):
    tenant = KeyConfig(key="bir_tenant", name="tenant")
    client, _ = app_with(mock, extra_keys=[tenant])
    async with client:
        response = await client.post(
            "/admin/keys", json={"name": "escalated", "admin": True},
            headers={"x-api-key": "bir_tenant"},
        )
    assert response.status_code == 403


async def test_unauthenticated_loopback_has_full_access(mock):
    """No keys configured at all is development mode, and stays fully open."""
    config = make_config(["a"])
    config.keys = []
    gateway = build(mock, config)
    from tokenbiryani.api.app import create_app

    app = create_app(config, gateway)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
    async with client:
        assert (await client.get("/admin/status")).status_code == 200


# ---- minting and revoking ------------------------------------------------------

async def test_create_returns_the_plaintext_once_and_stores_only_a_hash(gateway_factory, mock):
    gateway = gateway_factory(["a"])
    await gateway.startup()
    plaintext, record = await gateway.create_key("tenant-1", rpm=10)

    assert plaintext.startswith("bir_")
    assert "key_hash" not in record, "the redacted record must not carry the hash"
    stored = await gateway.store.list_keys()
    assert stored[0]["key_hash"] == hash_key(plaintext)
    assert plaintext not in str(stored), "the plaintext must never reach the store"


async def test_a_minted_key_authenticates_and_carries_its_scope(gateway_factory, mock):
    gateway = gateway_factory(["a", "b"])
    await gateway.startup()
    plaintext, _ = await gateway.create_key(
        "tenant-1", pool=["b"], priority="batch", rpm=99
    )

    result = gateway.keys.authenticate(plaintext)
    assert result.ok
    assert result.key.name == "tenant-1"
    assert result.key.pool == ["b"]
    assert result.key.priority == "batch"
    assert result.key.admin is False


async def test_a_minted_key_routes_within_its_pool(gateway_factory, mock):
    gateway = gateway_factory(["a", "b"])
    await gateway.startup()
    plaintext, _ = await gateway.create_key("tenant-1", pool=["b"])
    scoped = gateway.keys.authenticate(plaintext).key
    for _ in range(3):
        completion = await gateway.complete(body("x"), {}, scoped)
        assert completion.event.account_id == "b"


async def test_duplicate_names_are_rejected(gateway_factory, mock):
    gateway = gateway_factory(["a"])
    await gateway.startup()
    await gateway.create_key("tenant-1")
    with pytest.raises(GatewayError) as excinfo:
        await gateway.create_key("tenant-1")
    assert excinfo.value.status == 409
    with pytest.raises(GatewayError) as clash:
        await gateway.create_key("default")  # collides with the config key
    assert clash.value.status == 409


async def test_an_unknown_pool_account_is_rejected(gateway_factory, mock):
    gateway = gateway_factory(["a"])
    await gateway.startup()
    with pytest.raises(GatewayError) as excinfo:
        await gateway.create_key("tenant-1", pool=["ghost"])
    assert excinfo.value.status == 400
    assert "ghost" in excinfo.value.message


async def test_revoking_stops_authentication(gateway_factory, mock):
    gateway = gateway_factory(["a"])
    await gateway.startup()
    plaintext, _ = await gateway.create_key("tenant-1")
    assert gateway.keys.authenticate(plaintext).ok
    assert await gateway.revoke_key("tenant-1") is True
    assert not gateway.keys.authenticate(plaintext).ok


async def test_config_keys_cannot_be_revoked_over_the_api(gateway_factory, mock):
    gateway = gateway_factory(["a"])
    await gateway.startup()
    with pytest.raises(GatewayError) as excinfo:
        await gateway.revoke_key("default")
    assert excinfo.value.status == 409
    assert "config file" in excinfo.value.message


async def test_managed_keys_survive_a_config_reload(gateway_factory, mock):
    gateway = gateway_factory(["a", "b"])
    await gateway.startup()
    plaintext, _ = await gateway.create_key("tenant-1")
    gateway.reload(make_config(["a", "b"]))
    assert gateway.keys.authenticate(plaintext).ok, "a reload must not revoke live keys"


async def test_key_endpoints_end_to_end(mock):
    client, _ = app_with(mock)
    async with client:
        created = await client.post(
            "/admin/keys", json={"name": "tenant-1", "rpm": 5}, headers=ADMIN
        )
        assert created.status_code == 201
        minted = created.json()["key"]

        served = await client.post(
            "/v1/messages", json=body(), headers={"x-api-key": minted}
        )
        listed = await client.get("/admin/keys", headers=ADMIN)
        revoked = await client.delete("/admin/keys/tenant-1", headers=ADMIN)
        after = await client.post(
            "/v1/messages", json=body(), headers={"x-api-key": minted}
        )
        missing = await client.delete("/admin/keys/ghost", headers=ADMIN)

    assert served.status_code == 200
    names = {row["name"]: row for row in listed.json()["keys"]}
    assert names["tenant-1"]["source"] == "managed"
    assert names["tenant-1"]["key"] == "(hashed)"
    assert revoked.status_code == 200
    assert after.status_code == 401, "a revoked key must stop working"
    assert missing.status_code == 404


async def test_managed_keys_are_shared_between_instances(mock, tmp_path):
    """Instance B honours a key minted on instance A, through the shared store."""
    overrides = {"store": {"backend": "sqlite", "path": str(tmp_path / "state.db")}}
    first = build(mock, make_config(["a"], overrides=overrides))
    await first.startup()
    plaintext, _ = await first.create_key("tenant-1")

    second = build(mock, make_config(["a"], overrides=overrides))
    second._client = mock.client(base_url="https://mock.anthropic.test")
    await second.startup()
    assert second.keys.authenticate(plaintext).ok
    await first.aclose()
    await second.aclose()
