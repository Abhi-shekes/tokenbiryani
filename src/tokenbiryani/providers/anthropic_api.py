"""Anthropic API key upstream — the supported path."""

from __future__ import annotations

from typing import Dict

from ..config import AccountConfig
from .base import Upstream

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicUpstream(Upstream):
    def __init__(self, config: AccountConfig) -> None:
        super().__init__(config.id)
        self.config = config
        self.base_url = config.base_url.rstrip("/")

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
    raise ValueError(
        f"unsupported account type {config.type!r}. Shipped: anthropic_api. "
        "Bedrock and Vertex adapters are M7; implement providers.base.Upstream to add one."
        
    )
