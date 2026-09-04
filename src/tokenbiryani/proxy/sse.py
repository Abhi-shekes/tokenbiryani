"""Just enough SSE to account for a streamed response without altering it.

The gateway never rewrites the stream. It watches it go past, so it can tell when
the first content byte has been committed to the client and what the response cost.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Optional

#: The event that marks the point of no return: after this, the client has content
#: on screen and a failure can no longer be hidden by retrying elsewhere.
FIRST_CONTENT_EVENT = b"content_block_delta"


def iter_data_objects(text: str) -> Iterator[Dict[str, Any]]:
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            yield parsed


class UsageCollector:
    """Accumulates token usage from a Messages stream as it passes through."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0
        self.stop_reason: Optional[str] = None
        self._buffer = ""

    def feed(self, chunk: bytes) -> None:
        try:
            self._buffer += chunk.decode("utf-8")
        except UnicodeDecodeError:
            self._buffer += chunk.decode("utf-8", "ignore")
        # Only whole lines are parseable; keep the tail for the next chunk.
        head, newline, tail = self._buffer.rpartition("\n")
        if not newline:
            return
        self._buffer = tail
        self._absorb(head)

    def finish(self) -> None:
        if self._buffer:
            self._absorb(self._buffer)
            self._buffer = ""

    def _absorb(self, text: str) -> None:
        for obj in iter_data_objects(text):
            kind = obj.get("type")
            if kind == "message_start":
                usage = (obj.get("message") or {}).get("usage") or {}
                self.input_tokens += _int(usage.get("input_tokens"))
                self.output_tokens += _int(usage.get("output_tokens"))
                self.cache_read_tokens += _int(usage.get("cache_read_input_tokens"))
                self.cache_creation_tokens += _int(usage.get("cache_creation_input_tokens"))
            elif kind == "message_delta":
                usage = obj.get("usage") or {}
                self.output_tokens += _int(usage.get("output_tokens"))
                delta = obj.get("delta") or {}
                if delta.get("stop_reason"):
                    self.stop_reason = str(delta["stop_reason"])

    def as_body(self) -> Dict[str, Any]:
        return {
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_input_tokens": self.cache_read_tokens,
                "cache_creation_input_tokens": self.cache_creation_tokens,
            }
        }


def error_event(message: str, kind: str = "api_error") -> bytes:
    """An SSE error frame, for a failure that lands after the stream is committed."""
    payload = json.dumps({"type": "error", "error": {"type": kind, "message": message}})
    return ("event: error\ndata: " + payload + "\n\n").encode("utf-8")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
