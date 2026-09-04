"""`type: oauth` — an account backed by a Claude subscription session.

This is the core counterpart to `contrib/tokenbiryani-oauth`. The difference is
where the token comes from: contrib reads the one credentials file the Claude CLI
maintains, which is one subscription per machine. This one reads a token the gateway
obtained itself and holds per account, which is what pooling several requires.

**What an oauth account costs you, unchanged from contrib's README:** every routing
feature that distinguishes this project from a generic proxy is computed from
`anthropic-ratelimit-*` response headers, and subscription sessions do not send them.
Headroom routing, binding leases, admission control and the capacity horizon all go
dark for these accounts; failover and prompt-cache affinity remain. That is why
`observable_limits` is forced false for them rather than left to the operator.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Mapping, Optional

from ..config import AccountConfig
from .base import Upstream
from .oauth_credentials import TokenSource, build_source

#: Account options that name a token the gateway reads rather than owns. Any of
#: these means "do not use a gateway-managed session for this account".
EXTERNAL_SOURCE_OPTIONS = ("access_token", "token_env", "credentials_path")

logger = logging.getLogger("tokenbiryani")

DEFAULT_BASE_URL = "https://api.anthropic.com"

#: Unverified, and deliberately configurable. This project has never been run against
#: a real subscription session, so this is a starting value rather than a fact. If the
#: gateway logs 4xx mentioning a beta flag, set `options.beta_header`.
DEFAULT_BETA = "oauth-2025-04-20"

ANTHROPIC_VERSION = "2023-06-01"


class OAuthUpstream(Upstream):
    """Bearer-token upstream. The token is supplied by a callback, not held here.

    The gateway owns token lifecycle — storage, refresh, expiry — so this class asks
    for the current one on each request rather than caching a copy that could go
    stale behind a successful refresh.
    """

    def __init__(
        self,
        config: AccountConfig,
        token_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        super().__init__(config.id)
        self.config = config
        options: Dict[str, Any] = dict(config.options or {})
        self.base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        self.beta = str(options.get("beta_header") or DEFAULT_BETA)

        # Two ways an oauth account gets a token, and explicit config wins.
        #
        # `type: oauth` began in contrib/tokenbiryani-oauth, where it meant "read the
        # token the Claude CLI already wrote". Those configs must keep working
        # verbatim, so any account naming a source keeps using it; only accounts with
        # no source configured use the session the gateway logged in for and refreshes.
        self._source: Optional[TokenSource] = None
        if any(options.get(k) for k in EXTERNAL_SOURCE_OPTIONS):
            self._source = build_source(options)
        self._token_provider = token_provider

        if config.observable_limits:
            # Not merely cosmetic: an account whose headroom always reads full wins
            # every comparison against accounts reporting an honest partial budget,
            # so it would absorb the whole pool's traffic and then start 429ing.
            logger.warning(
                "account %r is an oauth account but observable_limits is true. "
                "Subscription sessions send no anthropic-ratelimit-* headers, so its "
                "headroom would read as permanently full. Forcing it false.",
                config.id,
            )
            config.observable_limits = False

    def token(self) -> str:
        if self._source is not None:
            # CredentialError propagates: the gateway classifies it as a transport
            # failure, and the reason lands in the request inspector.
            return self._source.token()
        if self._token_provider is None:
            raise RuntimeError(
                f"account {self.account_id!r} has no token: log in from the console, or "
                "set options.credentials_path / options.token_env / options.access_token"
            )
        return self._token_provider()

    def describe_source(self) -> str:
        if self._source is not None:
            return self._source.describe()
        return "session held by the gateway"

    def url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")

    def auth_headers(self) -> Dict[str, str]:
        headers = {
            "authorization": "Bearer " + self.token(),
            "content-type": "application/json",
            "anthropic-beta": self.beta,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        headers.update(self.config.headers)
        return headers

    def request_headers(self, client_headers: Mapping[str, str]) -> Dict[str, str]:
        headers = super().request_headers(client_headers)
        # A subscription session authenticates with a bearer token; an x-api-key
        # forwarded from the caller would be ignored at best and confusing at worst.
        for name in [k for k in headers if k.lower() == "x-api-key"]:
            headers.pop(name)
        client_beta = next(
            (v for k, v in client_headers.items() if k.lower() == "anthropic-beta"), None
        )
        if client_beta and self.beta not in client_beta:
            # Keep the caller's beta flags and add ours.
            headers["anthropic-beta"] = client_beta + "," + self.beta
        return headers
