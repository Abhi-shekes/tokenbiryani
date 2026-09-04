"""Google Vertex AI upstream.

Vertex serves Anthropic models over the same Messages shape and returns real SSE for
`:streamRawPredict`, so streaming needs no translation — only the URL and the auth
header differ.

    pip install "tokenbiryani[vertex]"

    accounts:
      - id: acct-vertex
        type: vertex
        options:
          project: my-project
          region: us-central1
          model_map:
            claude-test-1: claude-3-5-sonnet-v2@20241022
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Mapping, Optional

import httpx

from ..config import AccountConfig
from .base import Upstream, UpstreamResult, _as_result
from .translate import VERTEX_VERSION, map_model, to_platform_body

SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class VertexCredentials:
    """Wraps google-auth so the token is refreshed lazily and only once at a time."""

    def __init__(self, credentials: Optional[Any] = None) -> None:
        self._credentials = credentials
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._credentials is None:
                self._credentials = _default_credentials()
            credentials = self._credentials
            if not getattr(credentials, "valid", False):
                from google.auth.transport.requests import Request

                credentials.refresh(Request())
            return str(credentials.token)


def _default_credentials() -> Any:
    try:
        import google.auth
    except ImportError as exc:  # pragma: no cover - exercised by the error path
        raise RuntimeError(
            "the vertex adapter needs google-auth: pip install 'tokenbiryani[vertex]'"
        ) from exc
    credentials, _ = google.auth.default(scopes=[SCOPE])
    return credentials


class VertexUpstream(Upstream):
    def __init__(
        self, config: AccountConfig, credentials: Optional[VertexCredentials] = None
    ) -> None:
        super().__init__(config.id)
        self.config = config
        options = config.options or {}
        self.project = str(options.get("project") or "")
        self.region = str(options.get("region") or "us-central1")
        self.model_map: Dict[str, str] = dict(options.get("model_map") or {})
        if not self.project:
            raise ValueError(
                f"vertex account {config.id!r} needs options.project"
            )
        self.base_url = (config.base_url or "").rstrip("/") or (
            f"https://{self.region}-aiplatform.googleapis.com"
        )
        self._credentials = credentials or VertexCredentials()

    # ---- addressing ----------------------------------------------------------

    def model_url(self, model: str, stream: bool) -> str:
        verb = "streamRawPredict" if stream else "rawPredict"
        return (
            f"{self.base_url}/v1/projects/{self.project}/locations/{self.region}"
            f"/publishers/anthropic/models/{map_model(model, self.model_map)}:{verb}"
        )

    def url(self, path: str) -> str:
        # Vertex addresses the model, not a fixed path; only used for diagnostics.
        return self.base_url + "/" + path.lstrip("/")

    def auth_headers(self) -> Dict[str, str]:
        headers = {
            "authorization": "Bearer " + self._credentials.token(),
            "content-type": "application/json",
        }
        headers.update(self.config.headers)
        return headers

    def request_headers(self, client_headers: Mapping[str, str]) -> Dict[str, str]:
        headers = super().request_headers(client_headers)
        # anthropic-version is the API's own header and means nothing here; the
        # platform version travels in the body instead.
        headers.pop("anthropic-version", None)
        return headers

    # ---- transport -----------------------------------------------------------

    async def send(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: Mapping[str, Any],
        client_headers: Mapping[str, str],
        timeout: Optional[float] = None,
    ) -> UpstreamResult:
        model = str(payload.get("model") or "")
        response = await client.post(
            self.model_url(model, stream=False),
            json=to_platform_body(payload, VERTEX_VERSION),
            headers=self.request_headers(client_headers),
            timeout=timeout,
        )
        return _as_result(response)

    def open_stream(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: Mapping[str, Any],
        client_headers: Mapping[str, str],
        timeout: Optional[float] = None,
    ):
        model = str(payload.get("model") or "")
        return client.stream(
            "POST",
            self.model_url(model, stream=True),
            json=to_platform_body(payload, VERTEX_VERSION),
            headers=self.request_headers(client_headers),
            timeout=timeout,
        )
