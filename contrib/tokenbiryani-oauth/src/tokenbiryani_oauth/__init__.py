"""Deprecated. `type: oauth` is built into tokenbiryani itself.

This package added the account type before core had one. Core now ships `oauth`
as a built-in — including the credentials-file, environment-variable and static
token sources this package introduced, with identical option names — so every
configuration that worked here keeps working with core alone.

What core adds on top: a browser login (OAuth 2.0 + PKCE) that can hold a separate
session per account, and background refresh of those sessions. That is what makes
pooling more than one subscription possible; a single shared credentials file never
could. See ADR-0004.

Nothing here registers an account type any more — a plugin may not shadow a
built-in name. The names below are re-exported from core so existing imports keep
working, and this package can be uninstalled whenever convenient.
"""

from __future__ import annotations

import warnings

from tokenbiryani.providers.oauth import OAuthUpstream
from tokenbiryani.providers.oauth_credentials import (
    CredentialError,
    CredentialsFile,
    EnvToken,
    StaticToken,
    build_source,
)

warnings.warn(
    "tokenbiryani-oauth is deprecated: `type: oauth` is built into tokenbiryani. "
    "Uninstall this package; your existing configuration keeps working.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "OAuthUpstream",
    "CredentialError",
    "CredentialsFile",
    "EnvToken",
    "StaticToken",
    "build_source",
]
__version__ = "0.2.0"
