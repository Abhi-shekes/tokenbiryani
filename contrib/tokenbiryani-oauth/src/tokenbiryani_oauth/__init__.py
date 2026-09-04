"""Subscription-session account type for tokenbiryani.

Installing this package makes `type: oauth` a valid account type. Read the README
first: it explains what this does to routing quality, and the terms question.
"""

from __future__ import annotations

from .credentials import (
    CredentialError,
    CredentialsFile,
    EnvToken,
    StaticToken,
    build_source,
)
from .upstream import OAuthUpstream

__all__ = [
    "OAuthUpstream",
    "CredentialError",
    "CredentialsFile",
    "EnvToken",
    "StaticToken",
    "build_source",
]
__version__ = "0.1.0"
