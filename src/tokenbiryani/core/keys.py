"""Virtual keys.

Clients present a gateway key, never an upstream credential. Real keys stay in the
gateway process; a leaked virtual key costs you one revocation, not a rotation.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

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


def hash_key(key: str) -> str:
    """Managed keys are stored hashed. A leaked store is not a leaked key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def record_from_config(key: KeyConfig, plaintext: str) -> Dict[str, Any]:
    return {
        "name": key.name,
        "key_hash": hash_key(plaintext),
        "models": list(key.models),
        "pool": list(key.pool),
        "rpm": key.rpm,
        "spend_cap_usd": key.spend_cap_usd,
        "priority": key.priority,
        "max_wait_seconds": key.max_wait_seconds,
        "admin": key.admin,
        "created_at": time.time(),
    }


def _string_list(value: Any, default: List[str]) -> List[str]:
    """Records come back from a store as loose JSON; coerce rather than trust."""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return list(default)


def _optional_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def config_from_record(record: Mapping[str, Any]) -> KeyConfig:
    rpm = _optional_number(record.get("rpm"))
    return KeyConfig(
        key="",
        name=str(record.get("name") or ""),
        models=_string_list(record.get("models"), ["*"]),
        pool=_string_list(record.get("pool"), []),
        rpm=None if rpm is None else int(rpm),
        spend_cap_usd=_optional_number(record.get("spend_cap_usd")),
        priority=str(record.get("priority") or "interactive"),
        max_wait_seconds=_optional_number(record.get("max_wait_seconds")),
        admin=bool(record.get("admin")),
    )


class KeyRegistry:
    """Config keys are declared by the operator; managed keys are minted at runtime.

    Config keys are compared in plaintext (they live in the operator's own file).
    Managed keys are only ever stored as a hash, so the plaintext exists exactly once,
    in the response that created it.
    """

    def __init__(self, keys: List[KeyConfig]) -> None:
        self._keys = list(keys)
        self._managed: List[Dict[str, Any]] = []

    @property
    def keys(self) -> List[KeyConfig]:
        return list(self._keys)

    def set_managed(self, records: List[Dict[str, Any]]) -> None:
        self._managed = list(records)

    @property
    def open_access(self) -> bool:
        """No keys at all means loopback-only, unauthenticated. Refused remotely."""
        return not self._keys and not self._managed

    def authenticate(self, presented: Optional[str]) -> AuthResult:
        if self.open_access:
            # Unauthenticated loopback development: full access, including admin.
            return AuthResult(True, KeyConfig(key="", name="anonymous", admin=True))
        if not presented:
            return AuthResult(False, error="missing credentials")
        for candidate in self._keys:
            # Constant-time compare: key checking must not leak length or prefix.
            if hmac.compare_digest(candidate.key, presented):
                return AuthResult(True, candidate)
        digest = hash_key(presented)
        for record in self._managed:
            stored = str(record.get("key_hash") or "")
            if stored and hmac.compare_digest(stored, digest):
                return AuthResult(True, config_from_record(record))
        return AuthResult(False, error="invalid key")

    def by_name(self, name: str) -> Optional[KeyConfig]:
        for candidate in self._keys:
            if candidate.name == name:
                return candidate
        for record in self._managed:
            if record.get("name") == name:
                return config_from_record(record)
        return None

    def is_config_key(self, name: str) -> bool:
        return any(candidate.name == name for candidate in self._keys)

    def redacted(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for key in self._keys:
            out.append(
                {
                    "name": key.name,
                    "key": mask_key(key.key),
                    "source": "config",
                    "models": key.models,
                    "pool": key.pool or "all",
                    "rpm": key.rpm,
                    "spend_cap_usd": key.spend_cap_usd,
                    "priority": key.priority,
                    "admin": key.admin,
                }
            )
        for record in self._managed:
            key = config_from_record(record)
            out.append(
                {
                    "name": key.name,
                    "key": "(hashed)",
                    "source": "managed",
                    "models": key.models,
                    "pool": key.pool or "all",
                    "rpm": key.rpm,
                    "spend_cap_usd": key.spend_cap_usd,
                    "priority": key.priority,
                    "admin": key.admin,
                    "created_at": record.get("created_at"),
                }
            )
        return out
