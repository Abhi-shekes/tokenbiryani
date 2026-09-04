"""Where a subscription token comes from, when the gateway is not holding one.

An `oauth` account normally uses a session the gateway obtained itself and refreshes
(see `core/oauth.py`). These sources are the other case: a token that already exists
somewhere, which the gateway reads rather than owns.

The default is the credentials file the Claude Code CLI maintains. Reading it means
the gateway inherits a fresh token for free — the CLI does the refreshing. If that
token expires and nothing renews it, the account is disabled with a message telling
the operator to run the CLI once. An honest failure, and much better than a silent one.

These sources came from `contrib/tokenbiryani-oauth`, which is where `type: oauth`
started. They live in core now so that every configuration that package supported
keeps working unchanged.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence

#: Default location of the CLI's credentials file on Linux and macOS.
DEFAULT_PATH = os.path.join("~", ".claude", ".credentials.json")

#: Default dotted paths into that file. Both are configurable because this package
#: cannot verify the schema against a real login — run `tokenbiryani-oauth-check` to
#: see what your install actually writes.
DEFAULT_TOKEN_PATH = "claudeAiOauth.accessToken"
DEFAULT_EXPIRY_PATH = "claudeAiOauth.expiresAt"


class CredentialError(RuntimeError):
    """The token could not be read. The message is shown to the operator."""


def dig(data: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _as_epoch_seconds(value: Any) -> Optional[float]:
    """Expiry may be seconds or milliseconds; both appear in the wild."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    # Anything past the year 2200 in seconds is really milliseconds.
    return number / 1000.0 if number > 7_258_118_400 else number


class TokenSource:
    def token(self) -> str:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class StaticToken(TokenSource):
    """A token pasted into the config. Convenient, and it will expire on you."""

    def __init__(self, value: str) -> None:
        self._value = value

    def token(self) -> str:
        if not self._value:
            raise CredentialError("no token configured")
        return self._value

    def describe(self) -> str:
        return "static token from config"


class EnvToken(TokenSource):
    def __init__(self, variable: str) -> None:
        self.variable = variable

    def token(self) -> str:
        value = os.environ.get(self.variable, "")
        if not value:
            raise CredentialError(f"environment variable {self.variable} is not set")
        return value

    def describe(self) -> str:
        return f"environment variable {self.variable}"


class CredentialsFile(TokenSource):
    """Read the CLI's credentials file, re-reading whenever it changes on disk.

    Re-reading on mtime is what makes refresh someone else's problem: when the CLI
    renews the token, the next request picks it up without a restart.
    """

    def __init__(
        self,
        path: str = DEFAULT_PATH,
        token_path: str = DEFAULT_TOKEN_PATH,
        expiry_path: str = DEFAULT_EXPIRY_PATH,
        expiry_skew_seconds: float = 60.0,
    ) -> None:
        self.path = os.path.expanduser(path)
        self.token_path = token_path
        self.expiry_path = expiry_path
        self.expiry_skew_seconds = expiry_skew_seconds
        self._mtime: Optional[float] = None
        self._data: Dict[str, Any] = {}

    def load(self) -> Dict[str, Any]:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError as exc:
            raise CredentialError(
                f"cannot read {self.path}: {exc}. Sign in with the CLI first, or point "
                "options.credentials_path at the right file."
            ) from exc
        if mtime != self._mtime:
            try:
                with open(self.path, encoding="utf-8") as handle:
                    parsed = json.load(handle)
            except (OSError, ValueError) as exc:
                raise CredentialError(f"cannot parse {self.path}: {exc}") from exc
            if not isinstance(parsed, dict):
                raise CredentialError(f"{self.path} is not a JSON object")
            self._data = parsed
            self._mtime = mtime
        return self._data

    def expires_at(self) -> Optional[float]:
        return _as_epoch_seconds(dig(self.load(), self.expiry_path))

    def token(self) -> str:
        data = self.load()
        value = dig(data, self.token_path)
        if not isinstance(value, str) or not value:
            raise CredentialError(
                f"no token at {self.token_path!r} in {self.path}. Run "
                "`tokenbiryani-oauth-check` to see the keys this file actually has."
            )
        expiry = self.expires_at()
        if expiry is not None and expiry - self.expiry_skew_seconds <= time.time():
            raise CredentialError(
                f"the token in {self.path} expired at "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expiry))}. "
                "This package does not refresh tokens: run the CLI once to renew it, "
                "and the gateway will pick up the new token without a restart."
            )
        return value

    def describe(self) -> str:
        return f"credentials file {self.path}"


def build_source(options: Dict[str, Any]) -> TokenSource:
    """Pick a token source from an account's `options` block."""
    if options.get("access_token"):
        return StaticToken(str(options["access_token"]))
    if options.get("token_env"):
        return EnvToken(str(options["token_env"]))
    return CredentialsFile(
        path=str(options.get("credentials_path") or DEFAULT_PATH),
        token_path=str(options.get("token_path") or DEFAULT_TOKEN_PATH),
        expiry_path=str(options.get("expiry_path") or DEFAULT_EXPIRY_PATH),
        expiry_skew_seconds=float(options.get("expiry_skew_seconds") or 60.0),
    )


def redact(token: str) -> str:
    """Never print a token. Show just enough to tell two apart."""
    if len(token) < 16:
        return "•" * 8
    return token[:10] + "…" + token[-4:]


def key_shape(data: Any, prefix: str = "", depth: int = 0) -> Sequence[str]:
    """The dotted keys a credentials file contains, values omitted."""
    out: List[str] = []
    if isinstance(data, dict) and depth < 4:
        for name, value in data.items():
            path = f"{prefix}.{name}" if prefix else name
            if isinstance(value, dict):
                out.extend(key_shape(value, path, depth + 1))
            else:
                out.append(f"{path}: {type(value).__name__}")
    return out
