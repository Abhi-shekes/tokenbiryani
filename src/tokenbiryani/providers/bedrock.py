"""AWS Bedrock upstream.

Two things make Bedrock the awkward one. It addresses the model in the URL and signs
every request with SigV4, so the request cannot simply be forwarded. And it frames its
stream in AWS event-stream binary rather than SSE, so `iter_sse` decodes each frame
and re-emits the Anthropic event the client is expecting.

    pip install "tokenbiryani[bedrock]"

    accounts:
      - id: acct-bedrock
        type: bedrock
        options:
          region: us-east-1
          profile: default          # optional; omit for the ambient credential chain
          model_map:
            claude-test-1: anthropic.claude-3-5-sonnet-20241022-v2:0
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Mapping, Optional

import httpx

from ..config import AccountConfig
from .base import Upstream, UpstreamResult, _as_result
from .translate import BEDROCK_VERSION, map_model, to_platform_body

SERVICE = "bedrock"


class BedrockSigner:
    """SigV4 over botocore. Signing needs the body, so it happens per request."""

    def __init__(
        self, region: str, profile: Optional[str] = None, credentials: Optional[Any] = None
    ) -> None:
        self.region = region
        self.profile = profile
        self._credentials = credentials

    def credentials(self) -> Any:
        if self._credentials is None:
            try:
                from botocore.session import Session
            except ImportError as exc:  # pragma: no cover - error path only
                raise RuntimeError(
                    "the bedrock adapter needs botocore: "
                    "pip install 'tokenbiryani[bedrock]'"
                ) from exc
            session = Session(profile=self.profile) if self.profile else Session()
            resolved = session.get_credentials()
            if resolved is None:
                raise RuntimeError(
                    "no AWS credentials found for the bedrock adapter"
                )
            self._credentials = resolved.get_frozen_credentials()
        return self._credentials

    def sign(self, url: str, body: bytes, headers: Mapping[str, str]) -> Dict[str, str]:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        request = AWSRequest(method="POST", url=url, data=body, headers=dict(headers))
        SigV4Auth(self.credentials(), SERVICE, self.region).add_auth(request)
        return dict(request.headers)


class BedrockUpstream(Upstream):
    def __init__(self, config: AccountConfig, signer: Optional[BedrockSigner] = None) -> None:
        super().__init__(config.id)
        self.config = config
        options = config.options or {}
        self.region = str(options.get("region") or "us-east-1")
        self.model_map: Dict[str, str] = dict(options.get("model_map") or {})
        self.base_url = (config.base_url or "").rstrip("/") or (
            f"https://bedrock-runtime.{self.region}.amazonaws.com"
        )
        self._signer = signer or BedrockSigner(self.region, options.get("profile"))

    # ---- addressing ----------------------------------------------------------

    def model_url(self, model: str, stream: bool) -> str:
        verb = "invoke-with-response-stream" if stream else "invoke"
        return f"{self.base_url}/model/{map_model(model, self.model_map)}/{verb}"

    def url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")

    def auth_headers(self) -> Dict[str, str]:
        # Signing produces the auth headers; nothing static to add.
        return dict(self.config.headers)

    def _headers(self, client_headers: Mapping[str, str], url: str, body: bytes):
        headers = super().request_headers(client_headers)
        headers.pop("anthropic-version", None)
        headers["content-type"] = "application/json"
        headers["host"] = httpx.URL(url).host
        return self._signer.sign(url, body, headers)

    # ---- transport -----------------------------------------------------------

    async def send(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: Mapping[str, Any],
        client_headers: Mapping[str, str],
        timeout: Optional[float] = None,
    ) -> UpstreamResult:
        url = self.model_url(str(payload.get("model") or ""), stream=False)
        body = json.dumps(to_platform_body(payload, BEDROCK_VERSION)).encode("utf-8")
        response = await client.post(
            url, content=body, headers=self._headers(client_headers, url, body), timeout=timeout
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
        url = self.model_url(str(payload.get("model") or ""), stream=True)
        body = json.dumps(to_platform_body(payload, BEDROCK_VERSION)).encode("utf-8")
        return client.stream(
            "POST",
            url,
            content=body,
            headers=self._headers(client_headers, url, body),
            timeout=timeout,
        )

    async def iter_sse(self, response: httpx.Response):
        """Decode AWS event-stream frames and re-emit them as Anthropic SSE.

        Each frame's payload is `{"bytes": "<base64 of one Anthropic event>"}`. The
        gateway's stream machinery — the commit boundary, usage accounting — reads
        ordinary SSE, so the translation stops here.
        """
        buffer = _event_stream_buffer()
        async for chunk in response.aiter_bytes():
            buffer.add_data(chunk)
            for event in buffer:
                frame = _to_sse(event)
                if frame:
                    yield frame


def _event_stream_buffer():
    try:
        from botocore.eventstream import EventStreamBuffer
    except ImportError as exc:  # pragma: no cover - error path only
        raise RuntimeError(
            "the bedrock adapter needs botocore: pip install 'tokenbiryani[bedrock]'"
        ) from exc
    return EventStreamBuffer()


def _to_sse(event: Any) -> bytes:
    payload = getattr(event, "payload", None)
    if not payload:
        return b""
    try:
        wrapper = json.loads(payload.decode("utf-8"))
    except (ValueError, AttributeError):
        return b""
    encoded = wrapper.get("bytes")
    if not encoded:
        return b""
    try:
        inner = base64.b64decode(encoded)
        parsed = json.loads(inner.decode("utf-8"))
    except (ValueError, TypeError):
        return b""
    kind = parsed.get("type") or "message"
    return f"event: {kind}\ndata: {json.dumps(parsed)}\n\n".encode()
