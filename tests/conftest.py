from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from tokenbiryani.config import Config, KeyConfig  # noqa: E402
from tokenbiryani.core.gateway import Gateway  # noqa: E402
from tokenbiryani.testing.mock_upstream import MockAnthropic  # noqa: E402

BASE_URL = "https://mock.anthropic.test"


def make_config(
    account_ids: List[str],
    strategy: str = "sticky_headroom",
    overrides: Optional[Dict[str, Any]] = None,
    account_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Config:
    account_overrides = account_overrides or {}
    raw: Dict[str, Any] = {
        "server": {"host": "127.0.0.1", "port": 8787},
        "routing": {"strategy": strategy},
        # Deterministic and fast: no jittered sleeps between attempts in tests.
        "retry": {"backoff_base_seconds": 0.0, "backoff_max_seconds": 0.0},
        "accounts": [
            dict(
                {
                    "id": account_id,
                    "type": "anthropic_api",
                    "api_key": "key-" + account_id,
                    "base_url": BASE_URL,
                },
                **account_overrides.get(account_id, {})
            )
            for account_id in account_ids
        ],
        # The operator's own key in tests; /admin requires admin: true.
        "keys": [{"key": "bir_test", "name": "default", "admin": True}],
    }
    for section, values in (overrides or {}).items():
        raw.setdefault(section, {})
        if isinstance(raw[section], dict) and isinstance(values, dict):
            raw[section].update(values)
        else:
            raw[section] = values
    return Config.from_dict(raw)


@pytest.fixture
def mock() -> MockAnthropic:
    return MockAnthropic()


@pytest.fixture
def key() -> KeyConfig:
    return KeyConfig(key="bir_test", name="default")


def build(mock: MockAnthropic, config: Config) -> Gateway:
    """Wire a gateway to the mock upstream, one mock account per configured account."""
    for account in config.accounts:
        if mock.by_key(account.api_key) is None:
            mock.add(account.id, account.api_key)
    return Gateway(config, client=mock.client(base_url=BASE_URL))


@pytest.fixture
def gateway_factory(mock: MockAnthropic):
    created: List[Gateway] = []

    def factory(account_ids: List[str], **kwargs: Any) -> Gateway:
        gateway = build(mock, make_config(account_ids, **kwargs))
        created.append(gateway)
        return gateway

    yield factory


def body(prompt: str = "hello", stream: bool = False, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": "claude-test-1",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": prompt}],
    }
    if stream:
        payload["stream"] = True
    payload.update(extra)
    return payload


async def drain(iterator) -> bytes:
    chunks = []
    async for chunk in iterator:
        chunks.append(chunk)
    return b"".join(chunks)
