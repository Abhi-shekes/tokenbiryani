"""Tests for the subscription-session adapter, against the core mock upstream."""

from __future__ import annotations

import json
import logging
import os
import sys
import time

import httpx
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "src"))
sys.path.insert(0, os.path.join(ROOT, "src"))

from tokenbiryani.config import AccountConfig, Config, KeyConfig  # noqa: E402
from tokenbiryani.core.gateway import Gateway  # noqa: E402
from tokenbiryani.providers.anthropic_api import build_upstream, register_upstream  # noqa: E402

from tokenbiryani_oauth import OAuthUpstream  # noqa: E402
from tokenbiryani_oauth.check import main as check_main  # noqa: E402
from tokenbiryani_oauth.credentials import (  # noqa: E402
    CredentialError,
    CredentialsFile,
    EnvToken,
    StaticToken,
    build_source,
    redact,
)

BODY = {"model": "claude-x", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]}


def write_credentials(tmp_path, token="sk-ant-oat01-abcdefghijklmnop", expires_in=3600):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": token,
            "refreshToken": "sk-ant-ort01-zzzz",
            "expiresAt": int((time.time() + expires_in) * 1000),
            "subscriptionType": "max",
        }
    }))
    return str(path)


def account(**options):
    return AccountConfig(id="acct-oauth", type="oauth", observable_limits=False,
                         options=options)


# ---- credential sources --------------------------------------------------------

def test_reads_the_cli_credentials_file(tmp_path):
    source = CredentialsFile(write_credentials(tmp_path))
    assert source.token().startswith("sk-ant-oat01-")


def test_picks_up_a_refreshed_token_without_a_restart(tmp_path):
    path = write_credentials(tmp_path, token="first-token-aaaaaaaaaa")
    source = CredentialsFile(path)
    assert source.token() == "first-token-aaaaaaaaaa"

    time.sleep(0.01)
    write_credentials(tmp_path, token="second-token-bbbbbbbbb")
    os.utime(path, None)
    assert source.token() == "second-token-bbbbbbbbb", (
        "the CLI refreshes the file; re-reading on mtime is how we inherit that"
    )


def test_an_expired_token_says_what_to_do(tmp_path):
    source = CredentialsFile(write_credentials(tmp_path, expires_in=-10))
    with pytest.raises(CredentialError) as excinfo:
        source.token()
    assert "expired" in str(excinfo.value)
    assert "run the CLI once" in str(excinfo.value)


def test_a_missing_file_says_what_to_do(tmp_path):
    source = CredentialsFile(str(tmp_path / "nope.json"))
    with pytest.raises(CredentialError, match="Sign in with the CLI first"):
        source.token()


def test_an_unexpected_schema_points_at_the_check_command(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"somethingElse": {"token": "x"}}))
    with pytest.raises(CredentialError, match="tokenbiryani-oauth-check"):
        CredentialsFile(str(path)).token()


def test_expiry_accepts_seconds_or_milliseconds(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "t" * 20, "expiresAt": int(time.time() + 3600)}}))
    assert CredentialsFile(str(path)).token()


def test_source_selection(tmp_path, monkeypatch):
    assert isinstance(build_source({"access_token": "abc"}), StaticToken)
    assert isinstance(build_source({"token_env": "SOME_VAR"}), EnvToken)
    assert isinstance(build_source({}), CredentialsFile)
    monkeypatch.setenv("SOME_VAR", "from-env")
    assert build_source({"token_env": "SOME_VAR"}).token() == "from-env"


def test_tokens_are_never_printed_whole():
    token = "sk-ant-oat01-abcdefghijklmnopqrstuvwxyz"
    masked = redact(token)
    assert token not in masked
    assert masked.startswith("sk-ant-oa")
    assert redact("short") == "•" * 8


# ---- the upstream --------------------------------------------------------------

async def test_it_sends_a_bearer_token_and_no_api_key(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"id": "msg", "usage": {"input_tokens": 1}})

    upstream = OAuthUpstream(account(credentials_path=write_credentials(tmp_path)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await upstream.send(
            client, "/v1/messages", BODY, {"x-api-key": "sk-ant-should-be-dropped"}
        )

    assert result.status == 200
    assert seen["authorization"].startswith("Bearer sk-ant-oat01-")
    assert "x-api-key" not in seen, "a bearer session must not also present an api key"
    assert "oauth" in seen["anthropic-beta"]


async def test_it_keeps_the_callers_beta_flags(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"id": "msg"})

    upstream = OAuthUpstream(account(credentials_path=write_credentials(tmp_path)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await upstream.send(client, "/v1/messages", BODY, {"anthropic-beta": "some-flag-1"})

    assert "some-flag-1" in seen["anthropic-beta"]
    assert "oauth" in seen["anthropic-beta"]


def test_the_beta_header_is_configurable(tmp_path):
    upstream = OAuthUpstream(
        account(credentials_path=write_credentials(tmp_path), beta_header="custom-flag")
    )
    assert upstream.auth_headers()["anthropic-beta"] == "custom-flag"


def test_it_warns_when_limits_are_marked_observable(tmp_path, caplog):
    config = AccountConfig(
        id="acct-oauth", type="oauth", observable_limits=True,
        options={"credentials_path": write_credentials(tmp_path)},
    )
    with caplog.at_level(logging.WARNING, logger="tokenbiryani"):
        OAuthUpstream(config)
    assert "observable_limits" in caplog.text


def test_healthcheck_reports_a_usable_reason(tmp_path):
    good = OAuthUpstream(account(credentials_path=write_credentials(tmp_path)))
    assert good.healthcheck() is None
    bad = OAuthUpstream(account(credentials_path=str(tmp_path / "missing.json")))
    assert "Sign in with the CLI" in (bad.healthcheck() or "")


# ---- integration with the gateway ---------------------------------------------

def test_the_entry_point_target_is_constructible(tmp_path):
    """What the entry point resolves to must be callable with an AccountConfig."""
    built = OAuthUpstream(account(credentials_path=write_credentials(tmp_path)))
    assert built.account_id == "acct-oauth"


def test_registering_it_makes_the_type_valid(tmp_path):
    from tokenbiryani.providers import anthropic_api

    try:
        register_upstream("oauth", OAuthUpstream)
        built = build_upstream(account(credentials_path=write_credentials(tmp_path)))
        assert isinstance(built, OAuthUpstream)
    finally:
        anthropic_api._plugins.pop("oauth", None)


async def test_a_gateway_routes_through_it(tmp_path):
    """End to end: a pool of one subscription account serves a request."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        })

    from tokenbiryani.providers import anthropic_api

    register_upstream("oauth", OAuthUpstream)
    try:
        config = Config.from_dict({
            "accounts": [{
                "id": "acct-oauth", "type": "oauth", "observable_limits": False,
                "options": {"credentials_path": write_credentials(tmp_path)},
            }],
            "keys": [{"key": "bir_t", "name": "default", "admin": True}],
        })
        gateway = Gateway(
            config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        await gateway.startup()
        completion = await gateway.complete(
            BODY, {}, KeyConfig(key="bir_t", name="default", admin=True)
        )
        assert completion.status == 200
        assert completion.event.account_id == "acct-oauth"

        # The routing consequence, made concrete.
        account_runtime = gateway.accounts["acct-oauth"]
        assert account_runtime.mirror.headroom(time.time()) == 0.5, (
            "an unobservable account must not read as full"
        )
        assert gateway.capacity_horizon()["series"][0]["input_tokens"] == 0, (
            "it contributes nothing to a horizon it cannot see"
        )
        await gateway.aclose()
    finally:
        anthropic_api._plugins.pop("oauth", None)


# ---- the check command ---------------------------------------------------------

def test_check_reports_a_good_file(tmp_path, capsys):
    assert check_main(["--path", write_credentials(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "claudeAiOauth.accessToken: str" in out
    assert "observable_limits: false" in out
    assert "sk-ant-oat01-abcdefghijklmnop" not in out, "never print the whole token"


def test_check_reports_a_missing_file(tmp_path, capsys):
    assert check_main(["--path", str(tmp_path / "nope.json")]) == 1
    assert "unreadable" in capsys.readouterr().out


def test_check_shows_the_real_keys_when_the_schema_differs(tmp_path, capsys):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"oauthAccount": {"bearer": "x" * 30}}))
    assert check_main(["--path", str(path)]) == 1
    assert "oauthAccount.bearer: str" in capsys.readouterr().out
