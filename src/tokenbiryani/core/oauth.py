"""The OAuth 2.0 + PKCE login flow behind "Log in with Claude".

Why this exists in core when `contrib/tokenbiryani-oauth` already reads a token:
that package reads the *one* credentials file the Claude CLI maintains, which is
exactly one subscription per machine. Pooling — the entire point of this gateway —
needs several, each with its own session, and that means the gateway has to run the
login itself and hold the tokens. See ADR-0004.

Two things are worth knowing before reading further.

**The endpoints are configuration, not constants.** Anthropic does not document the
OAuth endpoints its first-party clients use, and this project has never been run
against a real subscription session. Rather than hard-code a guess that would fail
mysteriously, `oauth.client_id`, `oauth.authorize_url` and `oauth.token_url` are
required config with an error message that says so. The flow below is plain RFC 7636
and is correct whatever those values turn out to be.

**Refresh is implemented here, unlike in contrib.** That package deliberately leans
on the CLI to refresh its file. A gateway holding several sessions has no CLI to lean
on, so it refreshes them itself, ahead of expiry.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx


class OAuthError(RuntimeError):
    """A login could not be completed. The message is shown to the operator."""


def _b64url(raw: bytes) -> str:
    """base64url with the padding stripped, as PKCE requires."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_verifier() -> str:
    """A PKCE code verifier: 43-128 chars of unreserved characters (RFC 7636 §4.1)."""
    return _b64url(os.urandom(64))


def challenge_for(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass
class PendingLogin:
    """One in-flight authorization, held between `start` and `complete`."""

    state: str
    verifier: str
    account_id: str
    name: str
    started_at: float = field(default_factory=time.time)

    def expired(self, now: float, ttl: float = 900.0) -> bool:
        return now - self.started_at > ttl


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str = ""
    expires_at: Optional[float] = None
    scope: str = ""

    @classmethod
    def from_response(cls, payload: Dict[str, Any], now: float) -> OAuthTokens:
        access = str(payload.get("access_token") or "")
        if not access:
            raise OAuthError(
                "the token endpoint returned no access_token. Response keys: "
                + ", ".join(sorted(payload)) or "(empty body)"
            )
        expires_in = payload.get("expires_in")
        expires_at = None
        if expires_in is not None:
            try:
                expires_at = now + float(expires_in)
            except (TypeError, ValueError):
                expires_at = None
        return cls(
            access_token=access,
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=expires_at,
            scope=str(payload.get("scope") or ""),
        )

    def stale(self, now: float, skew: float = 300.0) -> bool:
        """True when this token should be refreshed. Unknown expiry is never stale."""
        return self.expires_at is not None and self.expires_at - skew <= now


class OAuthClient:
    """Drives the authorization-code + PKCE exchange against a configured provider."""

    def __init__(
        self,
        client_id: str,
        authorize_url: str,
        token_url: str,
        redirect_uri: str = "",
        scopes: Optional[list] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.client_id = client_id
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.redirect_uri = redirect_uri
        self.scopes = list(scopes or [])
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.authorize_url and self.token_url)

    def require_configured(self) -> None:
        if self.configured:
            return
        missing = [
            name
            for name, value in (
                ("oauth.client_id", self.client_id),
                ("oauth.authorize_url", self.authorize_url),
                ("oauth.token_url", self.token_url),
            )
            if not value
        ]
        raise OAuthError(
            "subscription login is not configured: "
            + ", ".join(missing)
            + " must be set in tokenbiryani.yaml. Anthropic does not publish the OAuth "
            "endpoints its first-party clients use, so this gateway will not guess at "
            "them — see docs/oauth.md for how to find the values your client uses."
        )

    def authorize_url_for(self, login: PendingLogin) -> str:
        self.require_configured()
        query = {
            "client_id": self.client_id,
            "response_type": "code",
            "code_challenge": challenge_for(login.verifier),
            "code_challenge_method": "S256",
            "state": login.state,
        }
        # A blank redirect_uri means the manual flow: the provider shows the code and
        # the operator pastes it. That works even when we cannot register a callback.
        if self.redirect_uri:
            query["redirect_uri"] = self.redirect_uri
        if self.scopes:
            query["scope"] = " ".join(self.scopes)
        separator = "&" if "?" in self.authorize_url else "?"
        return self.authorize_url + separator + urlencode(query)

    async def exchange(self, code: str, verifier: str) -> OAuthTokens:
        """Trade an authorization code for tokens."""
        return await self._post({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "code_verifier": verifier,
            **({"redirect_uri": self.redirect_uri} if self.redirect_uri else {}),
        })

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        if not refresh_token:
            raise OAuthError(
                "this account has no refresh token, so its session cannot be renewed. "
                "Log in again to replace it."
            )
        return await self._post({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        })

    async def _post(self, form: Dict[str, str]) -> OAuthTokens:
        self.require_configured()
        client = self._client or httpx.AsyncClient(timeout=30.0)
        owns = self._client is None
        try:
            response = await client.post(
                self.token_url,
                data=form,
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise OAuthError(f"could not reach {self.token_url}: {exc}") from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code >= 400:
            # OAuth errors are a documented shape; fall back to the raw body, capped,
            # so a provider that returns HTML does not flood the console.
            detail = response.text[:300]
            try:
                payload = response.json()
                detail = str(
                    payload.get("error_description") or payload.get("error") or detail
                )
            except ValueError:
                pass
            raise OAuthError(f"{self.token_url} returned {response.status_code}: {detail}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthError(f"{self.token_url} did not return JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise OAuthError(f"{self.token_url} returned {type(payload).__name__}, not an object")
        return OAuthTokens.from_response(payload, time.time())
