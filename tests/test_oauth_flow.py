"""Subscription login: the PKCE exchange, refresh, and what must never leak.

The endpoints themselves are configuration this project cannot verify, so these
tests script the token endpoint and pin the parts that are ours: the challenge,
the state handling, the refresh, and — most importantly — that neither token ever
appears in an API response.
"""

from __future__ import annotations

import base64
import hashlib
import time

import httpx
import pytest
from conftest import build, make_config

from tokenbiryani.core.gateway import GatewayError
from tokenbiryani.core.oauth import (
    OAuthClient,
    OAuthError,
    OAuthTokens,
    PendingLogin,
    challenge_for,
    make_verifier,
)


def oauth_client(handler, **overrides):
    settings = dict(
        client_id="cid", authorize_url="https://example.test/authorize",
        token_url="https://example.test/token",
    )
    settings.update(overrides)
    return OAuthClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                       **settings)


# ---- PKCE mechanics ------------------------------------------------------------

def test_the_verifier_and_challenge_follow_rfc7636():
    verifier = make_verifier()
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier and "+" not in verifier and "/" not in verifier
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge_for(verifier) == expected


def test_two_logins_never_share_a_verifier():
    assert len({make_verifier() for _ in range(50)}) == 50


def test_the_authorize_url_carries_the_challenge_not_the_verifier():
    """The verifier is the secret; only its hash may cross the wire."""
    login = PendingLogin(state="st", verifier=make_verifier(), account_id="a", name="A")
    url = oauth_client(lambda r: httpx.Response(200)).authorize_url_for(login)
    assert "code_challenge_method=S256" in url
    assert challenge_for(login.verifier) in url.replace("%3D", "=")
    assert login.verifier not in url, "the verifier must never leave the gateway"
    assert "state=st" in url


def test_a_redirect_uri_is_only_sent_when_configured():
    login = PendingLogin(state="st", verifier="v" * 43, account_id="a", name="A")
    assert "redirect_uri" not in oauth_client(lambda r: None).authorize_url_for(login)
    with_redirect = oauth_client(lambda r: None, redirect_uri="http://localhost/cb")
    assert "redirect_uri=" in with_redirect.authorize_url_for(login)


# ---- the exchange --------------------------------------------------------------

async def test_the_exchange_sends_the_verifier_and_reads_the_tokens():
    seen = {}

    def handler(request):
        seen.update(dict(pair.split("=", 1) for pair in request.content.decode().split("&")))
        return httpx.Response(200, json={
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
        })

    tokens = await oauth_client(handler).exchange("the-code", "the-verifier")
    assert seen["grant_type"] == "authorization_code"
    assert seen["code_verifier"] == "the-verifier"
    assert seen["code"] == "the-code"
    assert tokens.access_token == "at" and tokens.refresh_token == "rt"
    assert tokens.expires_at > time.time()


async def test_an_oauth_error_body_becomes_a_readable_message():
    def handler(request):
        return httpx.Response(400, json={
            "error": "invalid_grant", "error_description": "code already used"})

    with pytest.raises(OAuthError, match="code already used"):
        await oauth_client(handler).exchange("c", "v")


async def test_an_html_error_page_does_not_flood_the_console():
    def handler(request):
        return httpx.Response(502, text="<html>" + "x" * 5000 + "</html>")

    with pytest.raises(OAuthError) as caught:
        await oauth_client(handler).exchange("c", "v")
    assert len(str(caught.value)) < 500


async def test_a_response_without_an_access_token_is_an_error_not_a_blank_account():
    def handler(request):
        return httpx.Response(200, json={"token_type": "bearer"})

    with pytest.raises(OAuthError, match="no access_token"):
        await oauth_client(handler).exchange("c", "v")


def test_login_is_refused_until_the_endpoints_are_configured():
    """A guessed endpoint would look like a working feature and fail confusingly."""
    client = OAuthClient(client_id="", authorize_url="", token_url="")
    assert not client.configured
    with pytest.raises(OAuthError) as caught:
        client.require_configured()
    for name in ("oauth.client_id", "oauth.authorize_url", "oauth.token_url"):
        assert name in str(caught.value)


def test_staleness_is_only_claimed_when_expiry_is_known():
    now = 1000.0
    assert not OAuthTokens("a", expires_at=None).stale(now)
    assert OAuthTokens("a", expires_at=now + 60).stale(now, skew=300)
    assert not OAuthTokens("a", expires_at=now + 600).stale(now, skew=300)


# ---- through the gateway -------------------------------------------------------

def oauth_gateway(mock, tmp_path, handler, **oauth_overrides):
    config = make_config(["a"])
    config.store.backend = "sqlite"
    config.store.path = str(tmp_path / "state.db")
    config.store.secret_key_path = str(tmp_path / "secret.key")
    config.oauth.client_id = "cid"
    config.oauth.authorize_url = "https://example.test/authorize"
    config.oauth.token_url = "https://example.test/token"
    for name, value in oauth_overrides.items():
        setattr(config.oauth, name, value)
    gateway = build(mock, config)
    gateway._oauth = oauth_client(handler)
    return gateway


def token_handler(access="at", refresh="rt", expires_in=3600):
    def handler(request):
        return httpx.Response(200, json={
            "access_token": access, "refresh_token": refresh, "expires_in": expires_in})
    return handler


async def test_a_login_creates_a_named_unobservable_account(mock, tmp_path):
    gateway = oauth_gateway(mock, tmp_path, token_handler())
    await gateway.startup()

    started = await gateway.oauth_start("sub-01", "My subscription")
    assert "code_challenge" in started["authorize_url"]

    record = await gateway.oauth_complete(started["state"], "code-123")
    assert record["id"] == "sub-01"
    assert record["name"] == "My subscription"
    # Not the operator's choice: an account that reads as permanently full would
    # win every routing comparison against one reporting an honest budget.
    assert record["observable_limits"] is False
    assert gateway.accounts["sub-01"].config.type == "oauth"
    assert gateway.oauth_token("sub-01") == "at"
    await gateway.aclose()


async def test_neither_token_ever_appears_in_an_api_payload(mock, tmp_path):
    gateway = oauth_gateway(mock, tmp_path, token_handler(access="SECRET-AT",
                                                          refresh="SECRET-RT"))
    await gateway.startup()
    started = await gateway.oauth_start("sub-01", "s")
    record = await gateway.oauth_complete(started["state"], "c")

    for payload in (record, gateway.account_detail("sub-01"), gateway.snapshot()):
        blob = repr(payload)
        assert "SECRET-AT" not in blob, "the access token leaked"
        assert "SECRET-RT" not in blob, "the refresh token leaked"
    assert record["has_credential"] is True
    assert record["can_refresh"] is True

    # The detail endpoint reads from the runtime, not the store record, so it needs
    # its own path to the session facts — this is the one that got them wrong.
    detail = gateway.account_detail("sub-01")
    assert detail["can_refresh"] is True
    assert detail["session_expires_at"] is not None
    await gateway.aclose()


async def test_the_stored_session_is_encrypted_at_rest(mock, tmp_path):
    gateway = oauth_gateway(mock, tmp_path, token_handler(access="SECRET-AT",
                                                          refresh="SECRET-RT"))
    await gateway.startup()
    started = await gateway.oauth_start("sub-01", "s")
    await gateway.oauth_complete(started["state"], "c")
    await gateway.aclose()

    raw = (tmp_path / "state.db").read_bytes()
    assert b"SECRET-AT" not in raw
    assert b"SECRET-RT" not in raw


async def test_a_reused_or_unknown_state_is_refused(mock, tmp_path):
    """The state is single-use: a replayed callback must not mint a second account."""
    gateway = oauth_gateway(mock, tmp_path, token_handler())
    await gateway.startup()
    started = await gateway.oauth_start("sub-01", "s")
    await gateway.oauth_complete(started["state"], "c")

    with pytest.raises(GatewayError, match="not in progress"):
        await gateway.oauth_complete(started["state"], "c")
    with pytest.raises(GatewayError, match="not in progress"):
        await gateway.oauth_complete("never-issued", "c")
    await gateway.aclose()


async def test_an_expired_session_is_renewed_before_it_lapses(mock, tmp_path):
    calls = []

    def handler(request):
        form = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
        calls.append(form)
        return httpx.Response(200, json={
            "access_token": "fresh", "refresh_token": "rt2", "expires_in": 3600})

    gateway = oauth_gateway(mock, tmp_path, handler)
    await gateway.startup()
    started = await gateway.oauth_start("sub-01", "s")
    await gateway.oauth_complete(started["state"], "c")

    # Age the stored session past the refresh skew.
    records = {r["id"]: r for r in await gateway.store.list_accounts()}
    records["sub-01"]["oauth_expires_at"] = time.time() + 10
    await gateway.store.put_account(records["sub-01"])

    await gateway.refresh_oauth_sessions()
    assert calls[-1]["grant_type"] == "refresh_token"
    assert gateway.oauth_token("sub-01") == "fresh"
    await gateway.aclose()


async def test_a_provider_that_omits_a_new_refresh_token_keeps_the_old_one(mock, tmp_path):
    """Some providers issue a refresh token once. Dropping it would end the session."""
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:                       # the initial exchange
            return httpx.Response(200, json={
                "access_token": "at", "refresh_token": "rt", "expires_in": 3600})
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})

    gateway = oauth_gateway(mock, tmp_path, handler)
    await gateway.startup()
    started = await gateway.oauth_start("sub-01", "s")
    await gateway.oauth_complete(started["state"], "c")
    records = {r["id"]: r for r in await gateway.store.list_accounts()}
    records["sub-01"]["oauth_expires_at"] = time.time() + 10
    await gateway.store.put_account(records["sub-01"])

    await gateway.refresh_oauth_sessions()
    after = {r["id"]: r for r in await gateway.store.list_accounts()}["sub-01"]
    assert gateway.secrets.decrypt(after["oauth_refresh"]) == "rt", "the old token was dropped"
    await gateway.aclose()


async def test_a_session_that_cannot_be_renewed_disables_the_account(mock, tmp_path):
    """Better a visibly disabled account than one that reads ready and 401s."""
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    gateway = oauth_gateway(mock, tmp_path, handler)
    await gateway.startup()
    gateway._oauth = oauth_client(token_handler())
    started = await gateway.oauth_start("sub-01", "s")
    await gateway.oauth_complete(started["state"], "c")
    records = {r["id"]: r for r in await gateway.store.list_accounts()}
    records["sub-01"]["oauth_expires_at"] = time.time() + 10
    await gateway.store.put_account(records["sub-01"])

    gateway._oauth = oauth_client(handler)
    await gateway.refresh_oauth_sessions()
    assert "log in again" in (gateway.accounts["sub-01"].disabled_reason or "")
    await gateway.aclose()


async def test_an_account_with_no_session_fails_loudly_not_silently(mock, tmp_path):
    gateway = oauth_gateway(mock, tmp_path, token_handler())
    await gateway.startup()
    with pytest.raises(GatewayError, match="no active session"):
        gateway.oauth_token("nobody")
    await gateway.aclose()


async def test_a_login_cannot_shadow_an_existing_account(mock, tmp_path):
    gateway = oauth_gateway(mock, tmp_path, token_handler())
    await gateway.startup()
    with pytest.raises(GatewayError, match="already exists"):
        await gateway.oauth_start("a", "clash with the config account")
    await gateway.aclose()


def test_an_oauth_upstream_sends_a_bearer_token_and_strips_x_api_key():
    from tokenbiryani.config import AccountConfig
    from tokenbiryani.providers.oauth import OAuthUpstream

    config = AccountConfig(id="s", type="oauth", observable_limits=True)
    upstream = OAuthUpstream(config, lambda: "the-token")
    # Constructing it corrects the routing hazard rather than only warning.
    assert config.observable_limits is False

    headers = upstream.request_headers({"x-api-key": "sk-ant-leak", "anthropic-beta": "foo"})
    assert headers["authorization"] == "Bearer the-token"
    assert not any(k.lower() == "x-api-key" for k in headers)
    assert "foo" in headers["anthropic-beta"]


# ---- compatibility with the configuration contrib/tokenbiryani-oauth introduced ----

def build_oauth(options, token_provider=None):
    from tokenbiryani.config import AccountConfig
    from tokenbiryani.providers.anthropic_api import build_upstream

    return build_upstream(
        AccountConfig(id="sub", type="oauth", options=options), token_provider
    )


def test_a_credentials_file_account_needs_no_login(tmp_path):
    """`type: oauth` predates the login flow. Those configs must keep working."""
    import json

    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-from-file",
        "expiresAt": int((time.time() + 3600) * 1000),
    }}))
    upstream = build_oauth({"credentials_path": str(path)})
    assert upstream.token() == "sk-ant-oat01-from-file"


def test_a_configured_source_wins_over_a_gateway_session():
    """An upgrade must not silently move where an account's token comes from."""
    upstream = build_oauth({"access_token": "from-config"}, lambda: "from-gateway")
    assert upstream.token() == "from-config"


def test_an_account_with_neither_says_how_to_fix_it():
    upstream = build_oauth({})
    with pytest.raises(RuntimeError, match="log in from the console"):
        upstream.token()
