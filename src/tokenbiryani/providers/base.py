"""Upstream protocol.

An upstream is anything that can serve a Messages request and report its limits.
Adding Bedrock or Vertex means implementing this and nothing else.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

import httpx

#: Headers we never forward from the client — ours to set, or hop-by-hop.
BLOCKED_REQUEST_HEADERS = frozenset(
    {
        "host", "content-length", "connection", "keep-alive", "transfer-encoding",
        "upgrade", "te", "trailer", "proxy-authorization", "proxy-authenticate",
        "authorization", "x-api-key", "accept-encoding",
    }
)

#: Headers we never pass back to the client verbatim.
BLOCKED_RESPONSE_HEADERS = frozenset(
    {"content-length", "content-encoding", "connection", "transfer-encoding", "keep-alive"}
)


@dataclass
class UpstreamResult:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[Dict[str, Any]] = None
    raw: bytes = b""


def forwardable(headers: Mapping[str, str]) -> Dict[str, str]:
    """Pass the client's request headers through, minus the ones we own.

    The gateway is a router, not a rewriter: anything the client sent that isn't
    ours to control travels untouched, including beta flags we've never heard of.
    """
    return {k: v for k, v in headers.items() if k.lower() not in BLOCKED_REQUEST_HEADERS}


class Upstream(abc.ABC):
    """One credential's worth of capacity."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id

    @abc.abstractmethod
    def url(self, path: str) -> str:
        ...

    @abc.abstractmethod
    def auth_headers(self) -> Dict[str, str]:
        ...

    def request_headers(self, client_headers: Mapping[str, str]) -> Dict[str, str]:
        headers = forwardable(client_headers)
        headers.update(self.auth_headers())
        return headers

    async def send(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: Mapping[str, Any],
        client_headers: Mapping[str, str],
        timeout: Optional[float] = None,
    ) -> UpstreamResult:
        response = await client.post(
            self.url(path),
            json=payload,
            headers=self.request_headers(client_headers),
            timeout=timeout,
        )
        body: Optional[Dict[str, Any]]
        try:
            parsed = response.json()
            body = parsed if isinstance(parsed, dict) else None
        except ValueError:
            body = None
        return UpstreamResult(
            status=response.status_code,
            headers=dict(response.headers),
            body=body,
            raw=response.content,
        )

    def open_stream(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: Mapping[str, Any],
        client_headers: Mapping[str, str],
        timeout: Optional[float] = None,
    ):
        """Return an httpx streaming context manager; the caller drives it."""
        return client.stream(
            "POST",
            self.url(path),
            json=payload,
            headers=self.request_headers(client_headers),
            timeout=timeout,
        )
