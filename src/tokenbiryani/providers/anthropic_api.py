"""Anthropic API key upstream — the supported path."""

from __future__ import annotations

from typing import Dict

from ..config import AccountConfig
from .base import Upstream

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"


class AnthropicUpstream(Upstream):
    def __init__(self, config: AccountConfig) -> None:
        super().__init__(config.id)
        self.config = config
        self.base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")

    def url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")

    def auth_headers(self) -> Dict[str, str]:
        headers = {
            "x-api-key": self.config.api_key,
            "content-type": "application/json",
        }
        headers.update(self.config.headers)
        headers.setdefault("anthropic-version", ANTHROPIC_VERSION)
        return headers

    def request_headers(self, client_headers):
        headers = super().request_headers(client_headers)
        # The client's own anthropic-version wins if it sent one; ours is only a default.
        for name in list(headers):
            if name.lower() == "anthropic-version" and name != "anthropic-version":
                headers["anthropic-version"] = headers.pop(name)
        return headers


def build_upstream(config: AccountConfig) -> Upstream:
    if config.type in ("anthropic_api", "anthropic"):
        return AnthropicUpstream(config)
    if config.type == "bedrock":
        from .bedrock import BedrockUpstream

        return BedrockUpstream(config)
    if config.type == "vertex":
        from .vertex import VertexUpstream

        return VertexUpstream(config)
    raise ValueError(
        f"unsupported account type {config.type!r}; known: anthropic_api, bedrock, "
        "vertex. Implement providers.base.Upstream to add another."
    )
