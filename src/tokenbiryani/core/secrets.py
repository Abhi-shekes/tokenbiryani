"""Encryption for credentials the gateway stores itself.

Config-file accounts keep their credentials in the operator's own file, protected by
file permissions. Accounts added through the API are different: the gateway writes
them to a database that may be a shared Redis or a file that ends up in a backup. So
those are encrypted before they leave the process, and the key lives somewhere the
database does not.

Key resolution, in order:
  1. ``TOKENBIRYANI_SECRET_KEY`` — a urlsafe-base64 Fernet key. Right for containers.
  2. A key file beside the store, created 0600 on first use. Right for a laptop.

Losing the key means the stored credentials are unrecoverable, which is the point.
"""

from __future__ import annotations

import os
import stat
from typing import Optional

ENV_VAR = "TOKENBIRYANI_SECRET_KEY"
DEFAULT_KEY_FILE = "tokenbiryani.key"

#: Ciphertext is prefixed so a future scheme can be told apart from this one.
PREFIX = "v1:"


class SecretError(RuntimeError):
    """Raised when a secret cannot be encrypted or read back."""


def _fernet(key: bytes):
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - error path only
        raise SecretError(
            "storing account credentials needs the cryptography package: "
            "pip install 'tokenbiryani[secrets]'"
        ) from exc
    return Fernet(key)


def generate_key() -> bytes:
    # Guarded like _fernet: this is reached on the first attempt to store an
    # account, and a bare ModuleNotFoundError there tells the operator nothing
    # about which extra is missing.
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - error path only
        raise SecretError(
            "storing account credentials needs the cryptography package: "
            "pip install 'tokenbiryani[secrets]'"
        ) from exc

    return Fernet.generate_key()


def load_key(key_path: Optional[str] = None) -> bytes:
    """Find the key, or create one. Never returns a key from a world-readable file."""
    from_env = os.environ.get(ENV_VAR, "").strip()
    if from_env:
        return from_env.encode("utf-8")

    path = os.path.abspath(os.path.expanduser(key_path or DEFAULT_KEY_FILE))
    if os.path.exists(path):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise SecretError(
                f"{path} is readable by other users (mode {mode:o}). "
                "Run: chmod 600 " + path
            )
        with open(path, "rb") as handle:
            return handle.read().strip()

    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    key = generate_key()
    # Create with the right mode from the start; do not write then chmod.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
    return key


class SecretBox:
    """Encrypts and decrypts one kind of thing: a credential going into the store."""

    def __init__(self, key: Optional[bytes] = None, key_path: Optional[str] = None) -> None:
        self._key = key or load_key(key_path)
        self._cipher = _fernet(self._key)

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        return PREFIX + self._cipher.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        if not ciphertext.startswith(PREFIX):
            # A value written before encryption existed, or hand-edited into the
            # store. Accept it rather than lock the operator out of their own pool.
            return ciphertext
        from cryptography.fernet import InvalidToken

        try:
            return self._cipher.decrypt(ciphertext[len(PREFIX):].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise SecretError(
                "could not decrypt a stored credential — the secret key has changed. "
                "Restore the original key, or delete and re-add the account."
            ) from exc
