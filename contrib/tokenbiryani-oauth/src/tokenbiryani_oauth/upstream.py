"""A tokenbiryani account type backed by a subscription session.

What you get: failover across sessions, and prompt-cache affinity.
What you do not get: headroom-aware routing, leases that bind, admission control, or
the capacity horizon — all of which are computed from `anthropic-ratelimit-*` response
headers, which subscription sessions do not send.

That is not a shortcoming of this adapter; there is nothing to read. It is the reason
the gateway's own README recommends API keys for pooled capacity, and the reason these
accounts should be configured `observable_limits: false`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from tokenbiryani.config import AccountConfig
from tokenbiryani.providers.base import Upstream

from .credentials import CredentialError, TokenSource, build_source

logger = logging.getLogger("tokenbiryani")

DEFAULT_BASE_URL = "https://api.anthropic.com"

#: Unverified. This package has never been run against a real subscription session,
#: so the beta header is configurable and this is only a starting guess. If your
#: gateway logs 4xx with a message about the beta flag, set options.beta_header.
DEFAULT_BETA = "oauth-2025-04-20"

ANTHROPIC_VERSION = "2023-06-01"


class OAuthUpstream(Upstream):
    def __init__(self, config: AccountConfig, source: Optional[TokenSource] = None) -> None:
        super().__init__(config.id)
        self.config = config
        options: Dict[str, Any] = dict(config.options or {})
        self.base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        self.beta = str(options.get("beta_header") or DEFAULT_BETA)
        self.source = source or build_source(options)

        if config.observable_limits:
            logger.warning(
                "account %r is an oauth account but observable_limits is true. "
                "Subscription sessions send no anthropic-ratelimit-* headers, so its "
                "headroom will read as permanently full and it will win every "
                "comparison against accounts that report a real budget. Set "
                "observable_limits: false.",
                config.id,
            )

    def url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")

    def auth_headers(self) -> Dict[str, str]:
        # CredentialError propagates: the gateway classifies it as a transport failure
        # and moves on, and the message lands in the request inspector.
        token = self.source.token()
        headers = {
            "authorization": "Bearer " + token,
            "content-type": "application/json",
            "anthropic-beta": self.beta,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        headers.update(self.config.headers)
        return headers

    def request_headers(self, client_headers: Mapping[str, str]) -> Dict[str, str]:
        headers = super().request_headers(client_headers)
        # A subscription session authenticates with a bearer token; an x-api-key
        # forwarded from anywhere would be ignored at best and confusing at worst.
        for name in [k for k in headers if k.lower() == "x-api-key"]:
            headers.pop(name)
        client_beta = next(
            (v for k, v in client_headers.items() if k.lower() == "anthropic-beta"), None
        )
        if client_beta and self.beta not in client_beta:
            # Keep whatever beta flags the caller asked for, and add ours.
            headers["anthropic-beta"] = client_beta + "," + self.beta
        return headers

    def healthcheck(self) -> Optional[str]:
        """Return a reason this account cannot serve, or None. Used by the CLI."""
        try:
            self.source.token()
        except CredentialError as exc:
            return str(exc)
        return None
