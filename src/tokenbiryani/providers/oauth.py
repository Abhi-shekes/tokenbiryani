"""`type: oauth` — an account backed by a Claude subscription session.

The token normally comes from a login the gateway ran itself and holds per account,
which is what pooling more than one subscription requires. It can also come from a
credentials file or an environment variable — see `oauth_credentials` — for the case
where something else already obtained it.

**What an oauth account costs you:** a subscription session does not send the
`anthropic-ratelimit-{requests,input-tokens,output-tokens}-*` triples an API key
does, so `observable_limits` is forced false rather than left to the operator — an
account whose windows never populate would read as permanently full and win every
routing comparison.

It does send `anthropic-ratelimit-unified-*`: how much of the rolling 5-hour and
7-day windows is spent. That is a real headroom number and the mirror reads it, so
these accounts do get headroom-aware routing. What stays dark is anything needing an
absolute token count: leases cannot reserve against a percentage, and the capacity
horizon cannot plot one. Failover and prompt-cache affinity are unaffected.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Mapping, Optional

from ..config import AccountConfig
from .base import Upstream
from .oauth_credentials import CredentialError, StaticToken, TokenSource, build_source

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
        # `type: oauth` originally meant only "read the token the Claude CLI already
        # wrote". Those configs keep working verbatim, so any account naming a source
        # keeps using it; only accounts with no source configured use the session the
        # gateway logged in for and refreshes.
        self._source: Optional[TokenSource] = None
        if any(options.get(k) for k in EXTERNAL_SOURCE_OPTIONS):
            self._source = build_source(options)
        elif config.api_key:
            # A token pasted into the console — `claude setup-token` mints one that
            # outlives a CLI session. It arrives in `api_key` rather than in options
            # because that is the field the store encrypts and the admin API strips
            # by name; a secret in `options` would be neither.
            self._source = StaticToken(config.api_key)
        self._token_provider = token_provider

        if config.observable_limits:
            # Not merely cosmetic: an account whose classic windows never populate
            # reads as full, wins every comparison against accounts reporting an
            # honest partial budget, and absorbs the pool's traffic until it 429s.
            # Its real budget arrives as unified utilisation, which the mirror reads
            # whatever this flag says.
            logger.warning(
                "account %r is an oauth account but observable_limits is true. "
                "Subscription sessions send no per-window limit/remaining headers, so "
                "its headroom would read as permanently full. Forcing it false — its "
                "unified utilisation is read either way.",
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
                f"account {self.account_id!r} has no token: log in from the console, "
                "point it at this machine's Claude Code session, paste a token from "
                "`claude setup-token`, or set options.credentials_path / "
                "options.token_env / options.access_token"
            )
        return self._token_provider()

    def describe_source(self) -> str:
        if self._source is not None:
            return self._source.describe()
        return "session held by the gateway"

    def source_expiry(self) -> Optional[float]:
        """When the token this account reads goes stale, if the source knows.

        A credentials file states its own expiry, and showing it is the difference
        between "this will stop working at 21:56 unless the CLI renews it" and a
        401 nobody expected.
        """
        getter = getattr(self._source, "expires_at", None)
        if getter is None:
            return None
        try:
            return getter()
        except CredentialError:
            return None

    @property
    def source_kind(self) -> str:
        """Which of the four token sources this account is using, for the console."""
        if isinstance(self._source, StaticToken):
            return "token"
        if self._source is not None:
            return "external"
        return "login"

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
