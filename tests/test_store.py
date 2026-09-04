"""State stores, and the spend ledger that makes a cap mean something after a restart."""

from __future__ import annotations

import asyncio

import pytest
from conftest import body, build, make_config

from tokenbiryani.config import Config, KeyConfig
from tokenbiryani.core.gateway import GatewayError
from tokenbiryani.store.base import SCOPE_ACCOUNT, SCOPE_KEY, build_store
from tokenbiryani.store.memory import MemoryStateStore
from tokenbiryani.store.sqlite import SqliteStateStore


@pytest.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    if request.param == "memory":
        made = MemoryStateStore()
    else:
        made = SqliteStateStore(str(tmp_path / "state.db"))
    await made.startup()
    yield made
    await made.close()


# ---- interface parity: both backends must behave identically -------------------

async def test_affinity_round_trip(store):
    assert await store.get_affinity("fp:x") is None
    await store.set_affinity("fp:x", "acct-01", 60.0)
    assert await store.get_affinity("fp:x") == "acct-01"
    await store.clear_affinity("fp:x")
    assert await store.get_affinity("fp:x") is None


async def test_affinity_expires(store):
    await store.set_affinity("fp:x", "acct-01", 0.01)
    await asyncio.sleep(0.05)
    assert await store.get_affinity("fp:x") is None


async def test_affinity_is_overwritten_not_duplicated(store):
    await store.set_affinity("fp:x", "acct-01", 60.0)
    await store.set_affinity("fp:x", "acct-02", 60.0)
    assert await store.get_affinity("fp:x") == "acct-02"


async def test_key_request_counting_is_a_rolling_minute(store):
    now = 1_000_000.0
    for index in range(3):
        count = await store.record_key_request("k", now + index)
        assert count == index + 1
    # A request 61s later drops the earlier ones.
    assert await store.record_key_request("k", now + 61) == 3


async def test_spend_ledger_sums_within_the_window(store):
    await store.add_spend(SCOPE_KEY, "default", 1.5)
    await store.add_spend(SCOPE_KEY, "default", 2.5)
    assert await store.get_spend(SCOPE_KEY, "default", 3600.0) == pytest.approx(4.0)


async def test_spend_falls_out_of_the_window(store):
    await store.add_spend(SCOPE_KEY, "default", 5.0)
    await asyncio.sleep(0.05)
    assert await store.get_spend(SCOPE_KEY, "default", 0.01) == pytest.approx(0.0)


async def test_scopes_are_independent(store):
    await store.add_spend(SCOPE_KEY, "same-name", 1.0)
    await store.add_spend(SCOPE_ACCOUNT, "same-name", 2.0)
    assert await store.get_spend(SCOPE_KEY, "same-name", 3600.0) == pytest.approx(1.0)
    assert await store.get_spend(SCOPE_ACCOUNT, "same-name", 3600.0) == pytest.approx(2.0)


async def test_spend_by_scope_groups_by_name(store):
    await store.add_spend(SCOPE_ACCOUNT, "a", 1.0)
    await store.add_spend(SCOPE_ACCOUNT, "a", 0.5)
    await store.add_spend(SCOPE_ACCOUNT, "b", 2.0)
    totals = await store.spend_by_scope(SCOPE_ACCOUNT, 3600.0)
    assert totals == {"a": pytest.approx(1.5), "b": pytest.approx(2.0)}


# ---- persistence: the point of the exercise -----------------------------------

async def test_sqlite_survives_a_new_process(tmp_path):
    path = str(tmp_path / "state.db")
    first = SqliteStateStore(path)
    await first.startup()
    await first.set_affinity("fp:conv", "acct-01", 3600.0)
    await first.add_spend(SCOPE_ACCOUNT, "acct-01", 7.25)
    await first.close()

    second = SqliteStateStore(path)
    await second.startup()
    assert await second.get_affinity("fp:conv") == "acct-01"
    assert await second.get_spend(SCOPE_ACCOUNT, "acct-01", 3600.0) == pytest.approx(7.25)
    await second.close()


async def test_build_store_dispatch(tmp_path):
    memory = build_store(Config.from_dict({}))
    assert isinstance(memory, MemoryStateStore)
    sqlite = build_store(
        Config.from_dict({"store": {"backend": "sqlite", "path": str(tmp_path / "s.db")}})
    )
    assert isinstance(sqlite, SqliteStateStore)
    with pytest.raises(ValueError, match="unknown store backend"):
        build_store(Config.from_dict({"store": {"backend": "postgres"}}))


async def test_account_spend_is_restored_on_restart(mock, tmp_path):
    path = str(tmp_path / "state.db")
    overrides = {"store": {"backend": "sqlite", "path": path},
                 "pricing": {"claude-test-1": {"input": 1000.0, "output": 1000.0}}}
    key = KeyConfig(key="bir_test", name="default")

    first = build(mock, make_config(["a"], overrides=overrides))
    await first.startup()
    await first.complete(body(), {}, key)
    spent = first.accounts["a"].spend_usd
    assert spent > 0
    await first.aclose()

    # A fresh process against the same file must not hand the account a clean slate.
    second = build(mock, make_config(["a"], overrides=overrides))
    second._client = mock.client(base_url="https://mock.anthropic.test")
    await second.startup()
    assert second.accounts["a"].spend_usd == pytest.approx(spent)
    await second.aclose()


async def test_key_spend_cap_survives_a_restart(mock, tmp_path):
    path = str(tmp_path / "state.db")
    overrides = {"store": {"backend": "sqlite", "path": path},
                 "pricing": {"claude-test-1": {"input": 1_000_000.0, "output": 1_000_000.0}}}
    capped = KeyConfig(key="bir_test", name="default", spend_cap_usd=0.05)

    first = build(mock, make_config(["a"], overrides=overrides))
    await first.startup()
    await first.complete(body(), {}, capped)  # burns ~$120 of a $0.05 cap
    await first.aclose()

    second = build(mock, make_config(["a"], overrides=overrides))
    second._client = mock.client(base_url="https://mock.anthropic.test")
    await second.startup()
    with pytest.raises(GatewayError) as excinfo:
        await second.complete(body(), {}, capped)
    assert excinfo.value.status == 429
    assert "cap in the last" in excinfo.value.message
    await second.aclose()


async def test_a_cap_is_windowed_not_lifetime(mock, tmp_path):
    """A lifetime cap on a persistent store would wedge the gateway shut forever."""
    overrides = {
        "store": {"backend": "sqlite", "path": str(tmp_path / "state.db")},
        "spend": {"window_hours": 0.0000001},  # ~0.4ms
        "pricing": {"claude-test-1": {"input": 1_000_000.0, "output": 1_000_000.0}},
    }
    capped = KeyConfig(key="bir_test", name="default", spend_cap_usd=0.01)
    gateway = build(mock, make_config(["a"], overrides=overrides))
    await gateway.startup()
    await gateway.complete(body(), {}, capped)
    await asyncio.sleep(0.05)
    # The earlier spend has aged out of the window, so the cap no longer bites.
    completion = await gateway.complete(body(), {}, capped)
    assert completion.status == 200
    await gateway.aclose()
