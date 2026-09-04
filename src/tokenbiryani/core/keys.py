"""Virtual keys.

Clients present a gateway key, never an upstream credential. Real keys stay in the
gateway process; a leaked virtual key costs you one revocation, not a rotation.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..config import KeyConfig

PREFIX = "bir_"


#: Below this length a key has too little entropy to reveal any of it and stay secret.
MIN_MASKABLE = 16


def generate_key() -> str:
    return PREFIX + secrets.token_urlsafe(24)


def mask_key(key: str) -> str:
    """Show enough to tell two keys apart, never enough to use one.

    Short keys are masked completely rather than partially: revealing the first
    eight characters of an eight-character key reveals the key.
    """
    if not key:
        return ""
    if len(key) < MIN_MASKABLE:
        return "\u2022" * 8
    return key[: len(PREFIX) + 4] + "\u2026"


@dataclass
class AuthResult:
    ok: bool
    key: Optional[KeyConfig] = None
    error: str = ""
    status: int = 401


class KeyRegistry:
    def __init__(self, keys: List[KeyConfig]) -> None:
        self._keys = list(keys)

    @property
    def keys(self) -> List[KeyConfig]:
        return list(self._keys)

    @property
    def open_access(self) -> bool:
        """No keys configured means loopback-only, unauthenticated. Refused remotely."""
        return not self._keys

    def authenticate(self, presented: Optional[str]) -> AuthResult:
        if self.open_access:
            return AuthResult(True, KeyConfig(key="", name="anonymous"))
        if not presented:
            return AuthResult(False, error="missing credentials")
        for candidate in self._keys:
            # Constant-time compare: key checking must not leak length or prefix.
            if hmac.compare_digest(candidate.key, presented):
                return AuthResult(True, candidate)
        return AuthResult(False, error="invalid key")

    def by_name(self, name: str) -> Optional[KeyConfig]:
        for candidate in self._keys:
            if candidate.name == name:
                return candidate
        return None

    def redacted(self) -> List[Dict[str, object]]:
        out = []
        for key in self._keys:
            out.append(
                {
                    "name": key.name,
                    "key": mask_key(key.key),
                    "models": key.models,
                    "pool": key.pool or "all",
                    "rpm": key.rpm,
                    "spend_cap_usd": key.spend_cap_usd,
                }
            )
        return out
