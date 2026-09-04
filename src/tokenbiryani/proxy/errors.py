"""Error taxonomy.

Every upstream failure maps to exactly two decisions: what happens to the *account*,
and what happens to the *request*. Keeping them separate is what stops a client's
malformed 400 from being retried across every credential in the pool.
"""

from __future__ import annotations

import email.utils
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional


class AccountAction(str, Enum):
    NONE = "none"
    ERROR_TICK = "error_tick"
    COOLDOWN = "cooldown"
    SHORT_COOLDOWN = "short_cooldown"
    DISABLE = "disable"
    MARK_MODEL_UNSUPPORTED = "mark_model_unsupported"


class RequestAction(str, Enum):
    #: try a different account
    RETRY_OTHER = "retry_other"
    #: transient; the same account is allowed again
    RETRY_ANY = "retry_any"
    #: the client's problem, or unrecoverable — hand it back untouched
    RETURN = "return"


@dataclass(frozen=True)
class Classification:
    kind: str
    account_action: AccountAction
    request_action: RequestAction
    cooldown_seconds: Optional[float] = None
    detail: str = ""

    @property
    def retryable(self) -> bool:
        return self.request_action in (RequestAction.RETRY_OTHER, RequestAction.RETRY_ANY)


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """`retry-after` is either delta-seconds or an HTTP date. Accept both."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    reference = time.time() if now is None else now
    return max(0.0, when.timestamp() - reference)


def _error_type(body: Any) -> str:
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            return str(error.get("type") or "")
    return ""


def _error_message(body: Any) -> str:
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            return str(error.get("message") or "")
    return ""


def classify(
    status: int,
    headers: Optional[Mapping[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Classification:
    """Map an upstream response to an account action and a request action."""
    headers = headers or {}
    lowered = {k.lower(): v for k, v in headers.items()}
    detail = _error_message(body)
    etype = _error_type(body)

    if 200 <= status < 300:
        return Classification("ok", AccountAction.NONE, RequestAction.RETURN)

    if status == 429:
        return Classification(
            "rate_limit",
            AccountAction.COOLDOWN,
            RequestAction.RETRY_OTHER,
            cooldown_seconds=parse_retry_after(lowered.get("retry-after"), now),
            detail=detail,
        )

    if status == 529:
        return Classification(
            "overloaded",
            AccountAction.SHORT_COOLDOWN,
            RequestAction.RETRY_ANY,
            detail=detail,
        )

    if status in (401, 403):
        # A 403 that names the model is a capability gap, not a bad credential.
        if status == 403 and ("model" in detail.lower() or etype == "permission_error"):
            return Classification(
                "model_not_permitted",
                AccountAction.MARK_MODEL_UNSUPPORTED,
                RequestAction.RETRY_OTHER,
                detail=detail,
            )
        return Classification(
            "invalid_auth", AccountAction.DISABLE, RequestAction.RETRY_OTHER, detail=detail
        )

    if status == 404:
        return Classification(
            "not_found", AccountAction.NONE, RequestAction.RETURN, detail=detail
        )

    if status == 413 or etype in ("request_too_large", "invalid_request_error"):
        # Body-shaped problems belong to the caller. Never amplify them across the pool.
        return Classification(
            "invalid_request", AccountAction.NONE, RequestAction.RETURN, detail=detail
        )

    if status == 400:
        return Classification(
            "invalid_request", AccountAction.NONE, RequestAction.RETURN, detail=detail
        )

    if status in (408, 500, 502, 503, 504):
        return Classification(
            "upstream_error",
            AccountAction.ERROR_TICK,
            RequestAction.RETRY_ANY,
            detail=detail,
        )

    if 400 <= status < 500:
        return Classification(
            "client_error", AccountAction.NONE, RequestAction.RETURN, detail=detail
        )

    return Classification(
        "unknown", AccountAction.ERROR_TICK, RequestAction.RETRY_ANY, detail=detail
    )


def classify_exception(exc: BaseException) -> Classification:
    """Transport-level failures: no status, but the account still owes us an error tick."""
    return Classification(
        "transport",
        AccountAction.ERROR_TICK,
        RequestAction.RETRY_ANY,
        detail=f"{type(exc).__name__}: {exc}",
    )
