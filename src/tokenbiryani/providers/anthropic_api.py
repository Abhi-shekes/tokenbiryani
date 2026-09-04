"""Anthropic API key upstream — the supported path."""

from __future__ import annotations

import logging
from typing import Callable, Dict, List

from ..config import AccountConfig
from .base import Upstream

logger = logging.getLogger("tokenbiryani")

_plugins: Dict[str, Callable[[AccountConfig], Upstream]] = {}
_plugins_loaded = False


def register_upstream(name: str, factory: Callable[[AccountConfig], Upstream]) -> None:
    """Register an account type at runtime. Built-in names cannot be shadowed."""
    if name in BUILTIN_TYPES:
        raise ValueError(f"{name!r} is a built-in account type and cannot be replaced")
    _plugins[name] = factory


def plugin_upstreams() -> Dict[str, Callable[[AccountConfig], Upstream]]:
    global _plugins_loaded
    if _plugins_loaded:
        return _plugins
    _plugins_loaded = True
    for entry in _entry_points(PLUGIN_GROUP):
        try:
            register_upstream(entry.name, entry.load())
        except Exception as exc:  # noqa: BLE001 - a bad plugin must not be fatal
            logger.warning("could not load account type %r: %s", entry.name, exc)
    return _plugins


def _entry_points(group: str):
    """importlib.metadata changed shape in 3.10; support both."""
    from importlib.metadata import entry_points

    found = entry_points()
    selector = getattr(found, "select", None)
    if selector is not None:
        return list(selector(group=group))
    return list(found.get(group, []))


def available_types() -> List[str]:
    return sorted(set(BUILTIN_TYPES) | set(plugin_upstreams()))

#: Entry point group third-party packages publish account types under.
PLUGIN_GROUP = "tokenbiryani.providers"

BUILTIN_TYPES = ("anthropic_api", "bedrock", "vertex")

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
    """Construct the upstream for an account type, built-in or installed."""
    if config.type in ("anthropic_api", "anthropic"):
        return AnthropicUpstream(config)
    if config.type == "bedrock":
        from .bedrock import BedrockUpstream

        return BedrockUpstream(config)
    if config.type == "vertex":
        from .vertex import VertexUpstream

        return VertexUpstream(config)

    factory = plugin_upstreams().get(config.type)
    if factory is not None:
        return factory(config)

    raise ValueError(
        f"unsupported account type {config.type!r}; known: "
        f"{', '.join(available_types())}. Implement providers.base.Upstream and "
        f"publish it under the {PLUGIN_GROUP!r} entry point to add another."
    )
