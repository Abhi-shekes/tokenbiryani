"""Accounts added through the API: stored encrypted, merged into the live pool."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import BASE_URL, body, build, make_config

from tokenbiryani.api.app import create_app
from tokenbiryani.core.gateway import GatewayError
from tokenbiryani.core.secrets import SecretBox, SecretError

ADMIN = {"x-api-key": "bir_test"}


def sqlite_gateway(mock, tmp_path, account_ids=("acct-01",)):
    config = make_config(list(account_ids), overrides={"store": {
        "backend": "sqlite",
        "path": str(tmp_path / "state.db"),
        "secret_key_path": str(tmp_path / "secret.key"),
    }})
    return build(mock, config), config


# ---- encryption ----------------------------------------------------------------

def test_a_stored_credential_is_encrypted(tmp_path):
    box = SecretBox(key_path=str(tmp_path / "k.key"))
    ciphertext = box.encrypt("sk-ant-super-secret")
    assert "sk-ant-super-secret" not in ciphertext
    assert box.decrypt(ciphertext) == "sk-ant-super-secret"


def test_a_world_readable_key_file_is_refused(tmp_path):
    import os

    path = tmp_path / "k.key"
    SecretBox(key_path=str(path))
    os.chmod(path, 0o644)
    with pytest.raises(SecretError, match="readable by other users"):
        SecretBox(key_path=str(path))


def test_the_wrong_key_fails_loudly(tmp_path):
    first = SecretBox(key_path=str(tmp_path / "a.key"))
    ciphertext = first.encrypt("secret")
    second = SecretBox(key_path=str(tmp_path / "b.key"))
    with pytest.raises(SecretError, match="secret key has changed"):
        second.decrypt(ciphertext)


def test_plaintext_written_before_encryption_still_loads(tmp_path):
    """An operator must not be locked out of their own pool by an upgrade."""
    box = SecretBox(key_path=str(tmp_path / "k.key"))
    assert box.decrypt("sk-ant-plain") == "sk-ant-plain"


# ---- lifecycle -----------------------------------------------------------------

async def test_an_account_can_be_added_and_serves_traffic(mock, tmp_path, key):
    gateway, _ = sqlite_gateway(mock, tmp_path)
    await gateway.startup()
    mock.add("acct-new", "key-added")

    record = await gateway.create_account(
        "acct-new", "Work account", "key-added", base_url=BASE_URL
    )
    assert record["name"] == "Work account"
    assert "api_key" not in record, "the credential must not come back out"
    assert "acct-new" in gateway.accounts

    completion = await gateway.complete(
        body("x"), {}, type(key)(key="bir_test", name="default", admin=True, pool=["acct-new"])
    )
    assert completion.event.account_id == "acct-new"
    await gateway.aclose()


async def test_the_credential_never_lands_in_the_store_in_the_clear(mock, tmp_path):
    gateway, _ = sqlite_gateway(mock, tmp_path)
    await gateway.startup()
    await gateway.create_account("acct-new", "Work", "sk-ant-plaintext", base_url=BASE_URL)
    stored = json.dumps(await gateway.store.list_accounts())
    assert "sk-ant-plaintext" not in stored
    assert "v1:" in stored, "it should be encrypted, not merely absent"
    await gateway.aclose()


async def test_accounts_survive_a_restart(mock, tmp_path):
    gateway, _ = sqlite_gateway(mock, tmp_path)
    await gateway.startup()
    await gateway.create_account("acct-new", "Work", "key-added", base_url=BASE_URL)
    await gateway.aclose()

    again, _ = sqlite_gateway(mock, tmp_path)
    again._client = mock.client(base_url=BASE_URL)
    await again.startup()
    assert "acct-new" in again.accounts
    assert again.accounts["acct-new"].config.api_key == "key-added", "decrypted on load"
    await again.aclose()


async def test_duplicate_and_malformed_ids_are_refused(mock, tmp_path):
    gateway, _ = sqlite_gateway(mock, tmp_path)
    await gateway.startup()
    await gateway.create_account("acct-new", "Work", "k", base_url=BASE_URL)
    with pytest.raises(GatewayError) as clash:
        await gateway.create_account("acct-new", "Again", "k")
    assert clash.value.status == 409
    with pytest.raises(GatewayError) as bad:
        await gateway.create_account("has spaces/and-slashes", "Bad", "k")
    assert bad.value.status == 400
    with pytest.raises(GatewayError) as missing:
        await gateway.create_account("acct-two", "No key", "")
    assert missing.value.status == 400
    await gateway.aclose()


async def test_config_accounts_are_not_editable_by_the_api(mock, tmp_path):
    gateway, _ = sqlite_gateway(mock, tmp_path)
    await gateway.startup()
    with pytest.raises(GatewayError) as edit:
        await gateway.update_account("acct-01", name="renamed")
    assert edit.value.status == 409
    with pytest.raises(GatewayError) as remove:
        await gateway.delete_account("acct-01")
    assert remove.value.status == 409
    await gateway.aclose()


async def test_rename_and_rotate(mock, tmp_path):
    gateway, _ = sqlite_gateway(mock, tmp_path)
    await gateway.startup()
    await gateway.create_account("acct-new", "Old name", "key-old", base_url=BASE_URL)
    updated = await gateway.update_account(
        "acct-new", name="New name", api_key="key-rotated", cost_tier=2.0
    )
    assert updated["name"] == "New name"
    assert gateway.accounts["acct-new"].config.api_key == "key-rotated"
    assert gateway.accounts["acct-new"].config.cost_tier == 2.0
    await gateway.aclose()


async def test_rotating_a_credential_re_enables_a_disabled_account(mock, tmp_path):
    gateway, _ = sqlite_gateway(mock, tmp_path)
    await gateway.startup()
    await gateway.create_account("acct-new", "Work", "key-bad", base_url=BASE_URL)
    gateway.accounts["acct-new"].disabled_reason = "invalid_auth"
    await gateway.update_account("acct-new", api_key="key-good")
    assert gateway.accounts["acct-new"].disabled_reason is None
    await gateway.aclose()


async def test_delete_removes_it_from_the_pool(mock, tmp_path):
    gateway, _ = sqlite_gateway(mock, tmp_path)
    await gateway.startup()
    await gateway.create_account("acct-new", "Work", "k", base_url=BASE_URL)
    assert await gateway.delete_account("acct-new") is True
    assert "acct-new" not in gateway.accounts
    assert "acct-new" not in gateway.upstreams
    await gateway.aclose()


async def test_testing_a_credential_reports_the_reason(mock, tmp_path):
    gateway, _ = sqlite_gateway(mock, tmp_path)
    await gateway.startup()
    mock.add("acct-new", "key-added")
    await gateway.create_account("acct-new", "Work", "key-added", base_url=BASE_URL)
    good = await gateway.test_account("acct-new")
    assert good["ok"] is True and good["status"] == 200

    await gateway.update_account("acct-new", api_key="not-a-real-key")
    bad = await gateway.test_account("acct-new")
    assert bad["ok"] is False
    assert bad["status"] == 401
    await gateway.aclose()


async def test_a_config_reload_keeps_managed_accounts(mock, tmp_path):
    gateway, config = sqlite_gateway(mock, tmp_path)
    await gateway.startup()
    await gateway.create_account("acct-new", "Work", "k", base_url=BASE_URL)
    gateway.reload(make_config(["acct-01"]))
    assert "acct-new" in gateway.accounts, "the file does not govern managed accounts"
    await gateway.aclose()


# ---- over HTTP -----------------------------------------------------------------

async def test_the_admin_endpoints(mock, tmp_path):
    gateway, config = sqlite_gateway(mock, tmp_path)
    mock.add("acct-new", "key-added")
    app = create_app(config, gateway)
    await gateway.startup()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
    async with client:
        created = await client.post("/admin/accounts", headers=ADMIN, json={
            "id": "acct-new", "name": "Personal", "api_key": "key-added",
            "base_url": BASE_URL, "cost_tier": 1.5,
        })
        assert created.status_code == 201
        assert created.json()["name"] == "Personal"

        listed = await client.get("/admin/accounts", headers=ADMIN)
        sources = {a["id"]: a["source"] for a in listed.json()["accounts"]}
        assert sources == {"acct-01": "config", "acct-new": "managed"}

        tested = await client.post("/admin/accounts/acct-new/test", headers=ADMIN)
        assert tested.json()["ok"] is True

        patched = await client.patch(
            "/admin/accounts/acct-new", headers=ADMIN, json={"name": "Renamed"})
        assert patched.json()["name"] == "Renamed"

        refused = await client.delete("/admin/accounts/acct-01", headers=ADMIN)
        assert refused.status_code == 409

        deleted = await client.delete("/admin/accounts/acct-new", headers=ADMIN)
        assert deleted.status_code == 200
        missing = await client.delete("/admin/accounts/acct-new", headers=ADMIN)
        assert missing.status_code == 404
    await gateway.aclose()


async def test_a_tenant_key_cannot_add_an_account(mock, tmp_path):
    from tokenbiryani.config import KeyConfig

    gateway, config = sqlite_gateway(mock, tmp_path)
    config.keys.append(KeyConfig(key="bir_tenant", name="tenant"))
    gateway.keys = type(gateway.keys)(config.keys)
    app = create_app(config, gateway)
    await gateway.startup()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
    async with client:
        response = await client.post(
            "/admin/accounts", headers={"x-api-key": "bir_tenant"},
            json={"id": "sneaky", "api_key": "k"},
        )
    assert response.status_code == 403
    await gateway.aclose()
