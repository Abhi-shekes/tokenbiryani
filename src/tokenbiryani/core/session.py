"""Session affinity.

Anthropic's prompt cache is scoped per credential. A conversation that hops accounts
pays full price for a prefix it already cached somewhere else, so the router needs a
stable key for "this conversation" without the client having to supply one.

The fingerprint deliberately covers only the *stable* head of a request — system
prompt, tool definitions, and the opening messages — so it stays constant as a
conversation grows.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, List, Mapping, Optional

SESSION_HEADER = "x-tokenbiryani-session"

#: How many leading messages take part in the fingerprint. Enough to distinguish two
#: conversations that share a system prompt; few enough to survive history growth.
LEADING_MESSAGES = 2

#: Per-message character budget, so a long opening message can't dominate the hash.
MESSAGE_BUDGET = 512


def _flatten(content: Any, budget: int = MESSAGE_BUDGET) -> str:
    """Content is a string or a list of typed blocks. Reduce it to stable text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:budget]
    if isinstance(content, Mapping):
        kind = content.get("type", "")
        if kind == "text":
            return "text:" + str(content.get("text", ""))[:budget]
        if kind == "tool_use":
            return "tool_use:" + str(content.get("name", ""))
        if kind == "tool_result":
            return "tool_result:" + str(content.get("tool_use_id", ""))
        return str(kind)
    if isinstance(content, list):
        parts: List[str] = []
        remaining = budget
        for block in content:
            if remaining <= 0:
                break
            piece = _flatten(block, remaining)
            remaining -= len(piece)
            parts.append(piece)
        return "|".join(parts)
    return str(content)[:budget]


def fingerprint(body: Mapping[str, Any]) -> str:
    """Hash the stable prefix of a Messages request."""
    parts: List[str] = ["model=" + str(body.get("model", ""))]
    parts.append("system=" + _flatten(body.get("system"), budget=4096))

    tools = body.get("tools")
    if isinstance(tools, list):
        names = []
        for tool in tools:
            if isinstance(tool, Mapping):
                names.append(str(tool.get("name", "")))
        parts.append("tools=" + ",".join(sorted(names)))

    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages[:LEADING_MESSAGES]:
            if isinstance(message, Mapping):
                parts.append(
                    "{}:{}".format(message.get("role", ""), _flatten(message.get("content")))
                )

    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()
    return digest[:32]


def session_key(
    body: Mapping[str, Any],
    headers: Optional[Mapping[str, str]] = None,
    scope: str = "",
) -> str:
    """An explicit header always wins; otherwise fall back to the fingerprint.

    ``scope`` namespaces the result to one tenant. Without it the key is global, and
    two tenants collide in two ways that both matter: either can steer the other's
    affinity by choosing the same ``X-TokenBiryani-Session`` value, and two callers
    running the same agent share a fingerprint whenever their system prompt, tool
    names and opening messages match — which is the normal case for one popular
    client, not a contrived one. Pass the virtual key's name and neither happens.
    """
    if headers:
        for name, value in headers.items():
            if name.lower() == SESSION_HEADER and value.strip():
                return _scoped(scope, "hdr:" + value.strip()[:128])
    return _scoped(scope, "fp:" + fingerprint(body))


def _scoped(scope: str, key: str) -> str:
    """Prefix a session key with its tenant, when there is one."""
    if not scope:
        return key
    return "k:" + str(scope)[:64] + "|" + key


def cache_prefix_size(body: Mapping[str, Any]) -> int:
    """Rough size of the cacheable prefix — how much is at stake in a cache break."""
    try:
        return len(json.dumps(
            {k: body.get(k) for k in ("system", "tools") if body.get(k) is not None},
            ensure_ascii=False,
        ))
    except (TypeError, ValueError):
        return 0
