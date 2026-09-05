"""Hot reload must not launder an account's health."""

from __future__ import annotations

import time

import httpx
import pytest
from conftest import BASE_URL, body, build, make_config
from support.mock_upstream import rate_limit

from tokenbiryani.api.app import create_app
from tokenbiryani.core.account import AccountState
from tokenbiryani.core.gateway import GatewayError

AUTH = {"x-api-key": "bir_test"}


async def test_reload_adds_and_removes_accounts(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b"])
    result = gateway.reload(make_config(["b", "c"]))
    assert result["added"] == ["c"]
    assert result["removed"] == ["a"]
    assert set(gateway.accounts) == {"b", "c"}
    assert set(gateway.upstreams) == {"b", "c"}


async def test_surviving_accounts_keep_their_cooldown(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b"])
    gateway.accounts["a"].cooling_until = time.time() + 300
    gateway.accounts["a"].requests_total = 17
    gateway.reload(make_config(["a", "b"]))
    now = time.time()
    assert gateway.accounts["a"].state(now) is AccountState.COOLING
    assert gateway.accounts["a"].requests_total == 17, "counters must survive a reload"


async def test_rotated_credentials_re_enable_an_account(gateway_factory, mock, key):
    gateway = gateway_factory(["a"])
    gateway.accounts["a"].disabled_reason = "invalid_auth"
    fresh = make_config(["a"], account_overrides={"a": {"api_key": "key-a-rotated"}})
    gateway.reload(fresh)
    assert gateway.accounts["a"].disabled_reason is None
    assert gateway.upstreams["a"].config.api_key == "key-a-rotated"


async def test_reload_can_change_strategy(gateway_factory, mock, key):
    gateway = gateway_factory(["a", "b"])
    assert gateway.router.strategy == "sticky_headroom"
    gateway.reload(make_config(["a", "b"], strategy="round_robin"))
    assert gateway.router.strategy == "round_robin"


async def test_reload_without_a_config_path_is_rejected(gateway_factory, mock, key):
    gateway = gateway_factory(["a"])
    with pytest.raises(GatewayError) as excinfo:
        gateway.reload_from_path()
    assert excinfo.value.status == 400


async def test_a_broken_config_file_leaves_the_running_config_alone(
    gateway_factory, mock, tmp_path
):
    gateway = gateway_factory(["a"])
    broken = tmp_path / "tokenbiryani.yaml"
    broken.write_text("accounts:\n  - id: x\n    type: anthropic_api\n")  # no api_key
    gateway.config.path = str(broken)
    with pytest.raises(GatewayError):
        gateway.reload_from_path()
    assert set(gateway.accounts) == {"a"}, "the running pool must be untouched"


async def test_reload_from_a_real_file(gateway_factory, mock, tmp_path):
    gateway = gateway_factory(["a"])
    path = tmp_path / "tokenbiryani.yaml"
    path.write_text(
        "accounts:\n"
        "  - id: a\n    type: anthropic_api\n    api_key: key-a\n"
        f"    base_url: {BASE_URL}\n"
        "  - id: z\n    type: anthropic_api\n    api_key: key-z\n"
        f"    base_url: {BASE_URL}\n"
        "keys:\n  - key: bir_test\n    name: default\n"
    )
    gateway.config.path = str(path)
    result = gateway.reload_from_path()
    assert result["added"] == ["z"]


async def test_admin_reload_and_account_detail_endpoints(mock):
    config = make_config(["a", "b"])
    gateway = build(mock, config)
    app = create_app(config, gateway)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
    mock.script("a", rate_limit(20))
    mock.script("b", rate_limit(20))
    async with client:
        await client.post("/v1/messages", json=body(), headers=AUTH)
        detail = await client.get("/admin/accounts/a", headers=AUTH)
        missing = await client.get("/admin/accounts/nope", headers=AUTH)
        reload_response = await client.post("/admin/reload", headers=AUTH)

    assert missing.status_code == 404
    payload = detail.json()
    assert payload["id"] == "a"
    assert payload["recent_requests"], "an account's own failed attempts must show here"
    assert "error_kinds" in payload
    # No config path on a programmatically built gateway.
    assert reload_response.status_code == 400


async def test_error_kinds_are_counted(gateway_factory, mock, key):
    gateway = gateway_factory(
        ["a", "b"], strategy="priority", account_overrides={"a": {"priority": 1.0}}
    )
    mock.script("a", rate_limit(20))
    await gateway.complete(body(), {}, key)
    assert gateway.accounts["a"].error_kinds == {"rate_limit": 1}
