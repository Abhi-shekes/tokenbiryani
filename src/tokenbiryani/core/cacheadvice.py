"""Whether the caller ever asked for caching, and what it costs when they did not.

The gateway already measures the *outcome* — `cache_read_input_tokens` comes back on
every response and the console draws a hit rate from it. What it could not do is tell
the two reasons for a 0% hit rate apart:

    the client never marked a breakpoint  -> caching never engages at all, and no
                                             routing change can fix it
    the breakpoint is there, affinity broke -> a routing problem, already diagnosed

Both render as "cache hit 0%", and an operator seeing that reaches for the strategy
knob, which cannot help in the first case. Anthropic's cache only engages where the
request carries `cache_control`, so the presence of that marker is the fact that
separates them, and it is knowable before the request is even sent.

Nothing here keeps prompt content. It reads the body in memory, records booleans and
token counts, and drops it — the promise that bodies are never logged is not
weakened by knowing whether one had a breakpoint in it.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

#: Anthropic will not cache a prefix shorter than this, so a breakpoint on a small
#: request buys nothing and advising one would be noise. The real minimum is
#: model-dependent (larger for Haiku); this is the smaller of them, because
#: over-reporting a missing breakpoint is worse than staying quiet.
MIN_CACHEABLE_TOKENS = 1024

#: Rough characters per token, matching `limits.CHARS_PER_TOKEN`.
CHARS_PER_TOKEN = 3.5

#: Below this many requests a group is not worth an opinion.
MIN_REQUESTS_FOR_ADVICE = 20

#: A group with fewer than this fraction of requests marked is treated as unmarked.
BREAKPOINT_PRESENT = 0.5

CACHE_CONTROL = "cache_control"
EPHEMERAL = {"type": "ephemeral"}


def _blocks(value: Any) -> Iterable[Mapping[str, Any]]:
    """Yield the mapping blocks in a `system`, `tools` or message `content` field."""
    if isinstance(value, Mapping):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                yield item


def has_cache_breakpoint(body: Mapping[str, Any]) -> bool:
    """True when anything in the request is marked for caching.

    Checks the three places a breakpoint may legally sit: a system block, a tool
    definition, and a content block inside a message.
    """
    for field in ("system", "tools"):
        for block in _blocks(body.get(field)):
            if block.get(CACHE_CONTROL):
                return True
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            for block in _blocks(message.get("content")):
                if block.get(CACHE_CONTROL):
                    return True
    return False


def cacheable_prefix_tokens(body: Mapping[str, Any]) -> int:
    """Roughly how much of this request is a stable, cacheable head.

    The system prompt and the tool definitions: the part an agent resends unchanged
    on every turn, and the part worth a breakpoint.
    """
    characters = 0
    for field in ("system", "tools"):
        value = body.get(field)
        if isinstance(value, str):
            characters += len(value)
            continue
        for block in _blocks(value):
            for key in ("text", "description", "name"):
                piece = block.get(key)
                if isinstance(piece, str):
                    characters += len(piece)
            schema = block.get("input_schema")
            if isinstance(schema, Mapping):
                characters += len(str(schema))
    return int(characters / CHARS_PER_TOKEN)


def insert_cache_breakpoint(body: Mapping[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Return a copy of the body with one breakpoint at the end of the stable head.

    Off unless `cache.auto_breakpoint` is set, because this is the gateway editing a
    caller's request — the thing it otherwise refuses to do. It is the third
    documented exception, after the two fields Bedrock and Vertex need.

    The breakpoint goes on the last tool if there are tools, otherwise on the last
    system block; those are the end of the prefix an agent resends unchanged. A
    string `system` is promoted to a one-block list, which is the same prompt in the
    other legal spelling and the only way to attach the marker at all.
    """
    if has_cache_breakpoint(body):
        return dict(body), False
    if cacheable_prefix_tokens(body) < MIN_CACHEABLE_TOKENS:
        return dict(body), False

    updated = copy.deepcopy(dict(body))

    tools = updated.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        tools[-1][CACHE_CONTROL] = dict(EPHEMERAL)
        return updated, True

    system = updated.get("system")
    if isinstance(system, str) and system:
        updated["system"] = [
            {"type": "text", "text": system, CACHE_CONTROL: dict(EPHEMERAL)}
        ]
        return updated, True
    if isinstance(system, list) and system and isinstance(system[-1], dict):
        system[-1][CACHE_CONTROL] = dict(EPHEMERAL)
        return updated, True

    return updated, False


class _Group:
    """Counters for one (virtual key, model) pair. No content, only arithmetic."""

    __slots__ = (
        "requests", "marked", "prefix_tokens", "input_tokens",
        "cache_read_tokens", "cache_creation_tokens",
    )

    def __init__(self) -> None:
        self.requests = 0
        self.marked = 0
        self.prefix_tokens = 0
        self.input_tokens = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0


class CacheAdvisor:
    """Aggregates breakpoint presence against realised cache hits, per key and model."""

    def __init__(
        self,
        min_requests: int = MIN_REQUESTS_FOR_ADVICE,
        min_cacheable_tokens: int = MIN_CACHEABLE_TOKENS,
    ) -> None:
        self.min_requests = max(1, int(min_requests))
        self.min_cacheable_tokens = max(1, int(min_cacheable_tokens))
        self._groups: Dict[Tuple[str, str], _Group] = defaultdict(_Group)

    def reconfigure(self, min_requests: int) -> None:
        """Apply new settings without discarding the counters already gathered."""
        self.min_requests = max(1, int(min_requests))

    def observe(
        self,
        key_name: str,
        model: str,
        marked: Optional[bool],
        prefix_tokens: int,
        input_tokens: int,
        cache_read_tokens: int,
        cache_creation_tokens: int,
    ) -> None:
        if marked is None:
            return
        group = self._groups[(key_name or "", model or "")]
        group.requests += 1
        group.marked += 1 if marked else 0
        group.prefix_tokens += max(0, int(prefix_tokens))
        group.input_tokens += max(0, int(input_tokens))
        group.cache_read_tokens += max(0, int(cache_read_tokens))
        group.cache_creation_tokens += max(0, int(cache_creation_tokens))

    def advice(self) -> Dict[str, Any]:
        findings: List[Dict[str, Any]] = []
        for (key_name, model), group in sorted(self._groups.items()):
            if group.requests < self.min_requests:
                continue
            billed = (
                group.input_tokens + group.cache_read_tokens + group.cache_creation_tokens
            )
            marked_rate = group.marked / float(group.requests)
            mean_prefix = group.prefix_tokens // group.requests
            hit_rate = (group.cache_read_tokens / billed) if billed else 0.0

            findings.append({
                "key": key_name,
                "model": model,
                "requests": group.requests,
                "breakpoint_rate": round(marked_rate, 4),
                "mean_prefix_tokens": mean_prefix,
                "cache_hit_rate": round(hit_rate, 4),
                "uncached_input_tokens": group.input_tokens,
                "verdict": self._verdict(marked_rate, mean_prefix, hit_rate),
            })
        return {
            "min_requests": self.min_requests,
            "min_cacheable_tokens": self.min_cacheable_tokens,
            "findings": findings,
        }

    def _verdict(self, marked_rate: float, mean_prefix: int, hit_rate: float) -> str:
        """One sentence an operator can act on, or one saying there is nothing to do."""
        if marked_rate < BREAKPOINT_PRESENT:
            if mean_prefix < self.min_cacheable_tokens:
                return (
                    "no cache_control breakpoint, and the stable prefix is under "
                    f"{self.min_cacheable_tokens} tokens — too small to cache, so "
                    "there is nothing to fix here"
                )
            return (
                "no cache_control breakpoint on a stable prefix of about "
                f"{mean_prefix} tokens: caching never engages for this traffic, and "
                "no routing strategy can recover it. The client has to mark the "
                "prefix, or set cache.auto_breakpoint"
            )
        if hit_rate < 0.2:
            return (
                "breakpoints are being sent but the hit rate is low — this is a "
                "routing problem, not a client one. Check cache breaks and whether "
                "the owner keeps going cooling"
            )
        return "caching is engaged and working"
