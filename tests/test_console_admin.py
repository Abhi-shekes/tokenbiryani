"""The admin surface the console needs to configure a gateway without an editor.

Each of these existed only as a config-file edit before. That is a fine way to run a
server and a poor way to fix one at 3am, and two of them — a key scoped at a deleted
account, and a subscription whose credentials file is not the one you think — are
failures whose whole cost is how long it takes to find the file.
"""

from __future__ import annotations

import json

import httpx
from conftest import build, make_config

from tokenbiryani.api.app import create_app

ADMIN = {"x-api-key": "bir_test"}


def app_with(mock, tmp_path, account_ids=("acct-01", "acct-02")):
    """A sqlite-backed gateway: settings and overrides are meant to outlive a restart."""
    config = make_config(list(account_ids), overrides={"store": {
        "backend": "sqlite",
        "path": str(tmp_path / "state.db"),
        "secret_key_path": str(tmp_path / "secret.key"),
    }})
    gateway = build(mock, config)
    app = create_app(config, gateway)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
    return client, gateway, config


# ---- finding the machine's own Claude Code login -------------------------------

async def test_detect_reports_a_login_without_ever_returning_the_token(
    mock, tmp_path, monkeypatch
):
    home = tmp_path / "profile"
    home.mkdir()
    (home / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-SECRET",
        "refreshToken": "sk-ant-ort01-SECRET",
        "expiresAt": 4_102_444_800_000,
        "subscriptionType": "pro",
        "scopes": ["user:inference"],
    }}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))

    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        response = await client.get("/admin/oauth/detect", headers=ADMIN)
    assert response.status_code == 200
    found = [c for c in response.json()["candidates"] if c["exists"]]
    assert found, "the profile CLAUDE_CONFIG_DIR names must be offered first"
    assert found[0]["path"] == str(home / ".credentials.json")
    assert found[0]["subscription"] == "pro"
    assert found[0]["expired"] is False
    assert "SECRET" not in response.text, "a token must never reach the browser"


async def test_detect_finds_every_profile_on_the_machine(mock, tmp_path, monkeypatch):
    """A pool is several accounts, and several accounts is several config directories.

        alias claude-abhi='CLAUDE_CONFIG_DIR="$HOME/.claude-abhi" claude'

    That is how two Claude Code logins live on one machine. A scan that only knew
    about ~/.claude and the currently-exported one would find at most half a pool and
    leave the operator typing paths they have to go and look up.
    """
    home = tmp_path / "home"
    for name, token in (
        (".claude", "sk-ant-oat01-default"),
        (".claude-abhi", "sk-ant-oat01-abhi"),
        (".claude-mangesh", "sk-ant-oat01-mangesh"),
    ):
        profile = home / name
        profile.mkdir(parents=True)
        (profile / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": token, "expiresAt": 4_102_444_800_000,
            "subscriptionType": "pro",
        }}))
    # A stray file matching the glob is not a profile.
    (home / ".claude-notes.txt").write_text("not a directory")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        found = (await client.get("/admin/oauth/detect", headers=ADMIN)).json()["candidates"]

    paths = [c["path"] for c in found if c["exists"]]
    assert str(home / ".claude-abhi" / ".credentials.json") in paths
    assert str(home / ".claude-mangesh" / ".credentials.json") in paths
    assert str(home / ".claude" / ".credentials.json") in paths
    assert all("notes" not in path for path in paths)
    assert "sk-ant-oat01-abhi" not in json.dumps(found), "tokens never leave the process"


async def test_detect_says_which_login_is_stale_rather_than_hiding_it(
    mock, tmp_path, monkeypatch
):
    """The 401 that follows a stale file names neither file. This does."""
    home = tmp_path / "old"
    home.mkdir()
    (home / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-old", "expiresAt": 1_000_000_000_000,
    }}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))

    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        candidates = (await client.get("/admin/oauth/detect", headers=ADMIN)).json()
    stale = [c for c in candidates["candidates"] if c["exists"]][0]
    assert stale["expired"] is True
    assert "expired" in stale["detail"]


async def test_a_subscription_can_be_added_from_a_credentials_file(mock, tmp_path):
    """No OAuth endpoints, no paste: the account names a file and the gateway reads it."""
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-live", "expiresAt": 4_102_444_800_000,
    }}))
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        created = await client.post("/admin/accounts", headers=ADMIN, json={
            "id": "sub-01", "name": "My subscription", "type": "oauth",
            "options": {"credentials_path": str(path)},
        })
    assert created.status_code == 201
    assert created.json()["observable_limits"] is False, (
        "forced, not defaulted: classic windows that never populate read as full"
    )
    upstream = gateway.upstreams["sub-01"]
    assert upstream.token() == "sk-ant-oat01-live"
    assert upstream.source_kind == "external"


async def test_a_pasted_token_is_encrypted_like_any_other_credential(mock, tmp_path):
    """`claude setup-token` output is a secret, so it rides the field that is stored
    encrypted and stripped from every response by name."""
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        created = await client.post("/admin/accounts", headers=ADMIN, json={
            "id": "sub-02", "name": "Pasted", "type": "oauth",
            "api_key": "sk-ant-oat01-PASTED",
        })
        listed = await client.get("/admin/accounts", headers=ADMIN)
    assert created.status_code == 201
    assert "PASTED" not in created.text and "PASTED" not in listed.text
    stored = [r for r in await gateway.store.list_accounts() if r["id"] == "sub-02"][0]
    assert "PASTED" not in json.dumps(stored), "the store holds ciphertext"
    assert gateway.upstreams["sub-02"].token() == "sk-ant-oat01-PASTED"
    assert gateway.upstreams["sub-02"].source_kind == "token"


# ---- editing a key instead of reissuing it -------------------------------------

async def test_a_keys_pool_can_be_narrowed_without_breaking_its_clients(mock, tmp_path):
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        minted = await client.post("/admin/keys", headers=ADMIN, json={
            "name": "tenant", "pool": ["acct-01", "acct-02"], "rpm": 30,
        })
        key = minted.json()["key"]
        edited = await client.patch("/admin/keys/tenant", headers=ADMIN,
                                    json={"pool": ["acct-02"]})
    assert edited.status_code == 200
    assert edited.json()["pool"] == ["acct-02"]
    assert gateway.keys.by_name("tenant").pool == ["acct-02"]
    # The key itself is untouched, which is the whole reason not to reissue.
    assert gateway.keys.authenticate(key).key is not None


async def test_a_cap_can_be_lifted_as_well_as_set(mock, tmp_path):
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        await client.post("/admin/keys", headers=ADMIN,
                          json={"name": "capped", "spend_cap_usd": 5.0, "rpm": 10})
        lifted = await client.patch("/admin/keys/capped", headers=ADMIN,
                                    json={"spend_cap_usd": None, "rpm": None})
    assert lifted.json()["spend_cap_usd"] is None
    assert gateway.keys.by_name("capped").rpm is None


async def test_a_key_cannot_be_pointed_at_an_account_that_does_not_exist(mock, tmp_path):
    """The exact state that blocks a config reload, refused where it is made."""
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        await client.post("/admin/keys", headers=ADMIN, json={"name": "tenant"})
        refused = await client.patch("/admin/keys/tenant", headers=ADMIN,
                                     json={"pool": ["typo-01"]})
    assert refused.status_code == 400
    assert "typo-01" in refused.json()["error"]["message"]


async def test_a_config_key_belongs_to_the_file(mock, tmp_path):
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        refused = await client.patch("/admin/keys/default", headers=ADMIN,
                                     json={"rpm": 5})
    assert refused.status_code == 409
    assert "config file" in refused.json()["error"]["message"]


async def test_a_key_cannot_promote_itself_to_admin(mock, tmp_path):
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        await client.post("/admin/keys", headers=ADMIN, json={"name": "tenant"})
        refused = await client.patch("/admin/keys/tenant", headers=ADMIN,
                                     json={"admin": True})
    assert refused.status_code == 400
    assert gateway.keys.by_name("tenant").admin is False


# ---- gateway settings ----------------------------------------------------------

async def test_the_strategy_can_be_changed_and_says_where_it_came_from(mock, tmp_path):
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        before = (await client.get("/admin/settings", headers=ADMIN)).json()
        assert before["strategy"]["source"] == "config"

        changed = await client.post("/admin/settings", headers=ADMIN,
                                    json={"strategy": "round_robin"})
    assert changed.status_code == 200
    assert changed.json()["strategy"]["value"] == "round_robin"
    assert changed.json()["strategy"]["source"] == "console"
    assert changed.json()["strategy"]["file_value"] == "sticky_headroom"
    assert gateway.router.strategy == "round_robin"


async def test_changing_the_strategy_changes_how_accounts_are_scored(mock, tmp_path):
    """A strategy is a scorer and a set of weight multipliers, not a label.

    Renaming the field in place would leave the console reading `round_robin` while
    the router went on scoring like `sticky_headroom` — a setting that reports
    success and does nothing.
    """
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        assert gateway.router.spec.affinity == 1.0
        await client.post("/admin/settings", headers=ADMIN,
                          json={"strategy": "round_robin"})
    assert gateway.router.strategy == "round_robin"
    assert gateway.router.spec.affinity == 0.0, "round_robin ignores affinity"
    assert gateway.router.spec.rotate is True


async def test_an_unknown_strategy_is_refused_with_the_list(mock, tmp_path):
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        refused = await client.post("/admin/settings", headers=ADMIN,
                                    json={"strategy": "vibes"})
    assert refused.status_code == 400
    assert "sticky_headroom" in refused.json()["error"]["message"]
    assert gateway.router.strategy == "sticky_headroom"


async def test_a_console_setting_survives_a_reload_of_the_file(mock, tmp_path):
    """A setting is an overlay on the file. Saving an unrelated line must not undo it."""
    client, gateway, config = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        await client.post("/admin/settings", headers=ADMIN,
                          json={"strategy": "round_robin"})
        gateway.reload(config)
    assert gateway.router.strategy == "round_robin"


async def test_a_setting_outlives_the_process(mock, tmp_path):
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        await client.post("/admin/settings", headers=ADMIN,
                          json={"strategy": "cost_tiered"})
    restarted, _, _ = app_with(mock, tmp_path)
    async with restarted:
        pass
    fresh = build(mock, make_config(["acct-01"], overrides={"store": {
        "backend": "sqlite",
        "path": str(tmp_path / "state.db"),
        "secret_key_path": str(tmp_path / "secret.key"),
    }}))
    await fresh.startup()
    assert fresh.router.strategy == "cost_tiered"
    await fresh.aclose()


async def test_the_price_table_can_be_switched_from_the_console(mock, tmp_path):
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        switched = await client.post("/admin/settings", headers=ADMIN,
                                     json={"pricing_source": "builtin"})
    body = switched.json()
    assert body["pricing_source"]["value"] == "builtin"
    assert body["priced_as_of"], "the bundled table is dated, and the date is shown"
    assert gateway.config.price_for("claude-sonnet-5") is not None


async def test_an_unknown_setting_is_named_rather_than_ignored(mock, tmp_path):
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        refused = await client.post("/admin/settings", headers=ADMIN,
                                    json={"queue_max": 9000})
    assert refused.status_code == 400
    assert "queue_max" in refused.json()["error"]["message"]


# ---- overrides on config accounts ----------------------------------------------

async def test_a_config_account_can_be_disabled_and_handed_back(mock, tmp_path):
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        off = await client.patch("/admin/accounts/acct-01", headers=ADMIN,
                                 json={"enabled": False, "name": "Paused"})
        assert off.status_code == 200
        assert off.json()["overridden"] == ["enabled", "name"]
        assert gateway.accounts["acct-01"].config.enabled is False

        back = await client.delete("/admin/accounts/acct-01/override", headers=ADMIN)
    assert back.status_code == 200
    assert gateway.accounts["acct-01"].config.enabled is True
    assert gateway.accounts["acct-01"].config.name == ""


async def test_an_override_cannot_repoint_a_config_account(mock, tmp_path):
    """A file that says one thing while the gateway does another is worse than a 409."""
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        refused = await client.patch("/admin/accounts/acct-01", headers=ADMIN,
                                     json={"base_url": "https://elsewhere.test"})
    assert refused.status_code == 409
    assert gateway.accounts["acct-01"].config.base_url != "https://elsewhere.test"


async def test_clearing_an_override_on_a_managed_account_is_a_404(mock, tmp_path):
    client, gateway, _ = app_with(mock, tmp_path)
    async with client:
        await gateway.startup()
        await client.post("/admin/accounts", headers=ADMIN, json={
            "id": "managed-01", "name": "Managed", "api_key": "sk-ant-x",
        })
        response = await client.delete("/admin/accounts/managed-01/override", headers=ADMIN)
    assert response.status_code == 404
