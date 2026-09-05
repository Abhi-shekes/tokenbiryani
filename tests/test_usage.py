"""Persisted usage history: the store parity, and the arithmetic behind the charts.

The event log is a 500-entry ring in memory, so before this existed a restart
erased every chart. These tests hold all three backends to one behaviour and pin
the aggregation the console draws.
"""

from __future__ import annotations

import time

import pytest
from conftest import body, build, make_config
from test_store import fake_redis_store

from tokenbiryani.observability.usage import (
    aggregate,
    resolve_bucket,
    resolve_window,
    sample_from_event,
)
from tokenbiryani.store.memory import MemoryStateStore
from tokenbiryani.store.sqlite import SqliteStateStore


@pytest.fixture(params=["memory", "sqlite", "redis"])
async def store(request, tmp_path):
    if request.param == "memory":
        made = MemoryStateStore()
    elif request.param == "sqlite":
        made = SqliteStateStore(str(tmp_path / "usage.db"))
    else:
        made, _ = fake_redis_store()
    await made.startup()
    yield made
    await made.close()


def row(at, account="acct-01", **overrides):
    sample = {
        "at": at,
        "account_id": account,
        "key_name": "tenant",
        "model": "claude-test-1",
        "status": 200,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": 0.01,
        "latency": 0.5,
        "affinity_broken": 0,
        "via": "messages",
        "error": "",
    }
    sample.update(overrides)
    return sample


# ---- interface parity: all three backends must behave identically --------------

async def test_a_usage_row_round_trips(store):
    now = time.time()
    await store.record_usage(row(now))
    rows = await store.usage_rows(now - 60, now + 60)
    assert len(rows) == 1
    assert rows[0]["account_id"] == "acct-01"
    assert rows[0]["input_tokens"] == 100
    assert rows[0]["cost_usd"] == pytest.approx(0.01)


async def test_the_window_is_half_open(store):
    """[since, until) — a row exactly on the boundary belongs to the next window."""
    now = int(time.time())
    await store.record_usage(row(now))
    assert await store.usage_rows(now, now + 10) != []
    assert await store.usage_rows(now + 1, now + 10) == []
    assert await store.usage_rows(now - 10, now) == [], "until is exclusive"


async def test_rows_can_be_filtered_to_one_account(store):
    now = time.time()
    await store.record_usage(row(now, account="acct-01"))
    await store.record_usage(row(now, account="acct-02"))
    both = await store.usage_rows(now - 60, now + 60)
    one = await store.usage_rows(now - 60, now + 60, "acct-02")
    assert len(both) == 2
    assert [r["account_id"] for r in one] == ["acct-02"]


async def test_identical_rows_in_the_same_instant_are_both_kept(store):
    """Redis stores members in a set; two identical requests must not collapse."""
    now = time.time()
    await store.record_usage(row(now))
    await store.record_usage(row(now))
    assert len(await store.usage_rows(now - 60, now + 60)) == 2


# ---- the aggregation the charts are drawn from --------------------------------

def test_buckets_cover_the_whole_window_even_when_empty():
    """A gap in traffic must draw as a gap, not compress the time axis."""
    now = 1_000_000.0
    result = aggregate([row(now - 30)], now, window_seconds=3600, bucket_seconds=300)
    assert len(result["buckets"]) == 12
    assert sum(b["all"]["requests"] for b in result["buckets"]) == 1
    assert [b["all"]["requests"] for b in result["buckets"]].count(0) == 11


def test_buckets_are_ordered_and_evenly_spaced():
    now = 1_000_000.0
    buckets = aggregate([], now, 3600, 300)["buckets"]
    times = [b["at"] for b in buckets]
    assert times == sorted(times)
    assert {b - a for a, b in zip(times, times[1:])} == {300}


def test_rows_outside_the_window_are_ignored():
    now = 1_000_000.0
    rows = [row(now - 30), row(now - 99_999), row(now + 99_999)]
    result = aggregate(rows, now, 3600, 300)
    assert result["overall"]["requests"] == 1


def test_grouping_splits_by_account_model_or_key():
    now = 1_000_000.0
    rows = [
        row(now - 10, account="acct-01", model="opus"),
        row(now - 10, account="acct-02", model="opus"),
        row(now - 10, account="acct-02", model="sonnet"),
    ]
    by_account = aggregate(rows, now, 3600, 3600, group_by="account")
    assert by_account["totals"]["acct-02"]["requests"] == 2

    by_model = aggregate(rows, now, 3600, 3600, group_by="model")
    assert by_model["totals"]["opus"]["requests"] == 2
    assert by_model["totals"]["sonnet"]["requests"] == 1

    by_key = aggregate(rows, now, 3600, 3600, group_by="key")
    assert by_key["totals"]["tenant"]["requests"] == 3


def test_cache_hit_rate_matches_the_event_log_definition():
    """cache_read / (input + cache_read + cache_write) — billed input, not raw."""
    now = 1_000_000.0
    rows = [row(now - 10, input_tokens=100, cache_read_tokens=300, cache_creation_tokens=0)]
    overall = aggregate(rows, now, 3600, 3600)["overall"]
    assert overall["billed_input_tokens"] == 400
    assert overall["cache_hit_rate"] == pytest.approx(0.75)


def test_a_quiet_bucket_reports_no_cache_rate_rather_than_zero():
    """Zero would draw as 'every request missed', which is a different claim."""
    now = 1_000_000.0
    buckets = aggregate([], now, 3600, 300)["buckets"]
    assert all(b["all"]["cache_hit_rate"] is None for b in buckets)
    assert all(b["all"]["latency_avg"] is None for b in buckets)


def test_errors_are_counted_from_status_and_from_a_missing_one():
    now = 1_000_000.0
    rows = [
        row(now - 10, status=200),
        row(now - 10, status=429),
        row(now - 10, status=0, error="api_error"),   # never reached an upstream
    ]
    overall = aggregate(rows, now, 3600, 3600)["overall"]
    assert overall["requests"] == 3
    assert overall["errors"] == 2


def test_groups_are_ordered_largest_first():
    """Legend order and stacking order have to agree."""
    now = 1_000_000.0
    rows = [row(now - 10, account="small", input_tokens=1)] + [
        row(now - 10, account="big", input_tokens=1000) for _ in range(3)
    ]
    assert aggregate(rows, now, 3600, 3600)["groups"] == ["big", "small"]


def test_window_and_bucket_resolution():
    assert resolve_window("24h") == 86400
    assert resolve_window("7d") == 604800
    assert resolve_window(None) == 86400
    assert resolve_window("nonsense") == 86400
    assert resolve_window("999999999") == 2592000, "clamped to the retention window"
    assert resolve_bucket(3600, None) == 300
    assert resolve_bucket(86400, None) == 3600
    # Never so many marks that the chart becomes a comb.
    assert 3600 // resolve_bucket(3600, "1") <= 240


# ---- end to end: a real request lands in the history ---------------------------

async def test_a_served_request_is_persisted_and_survives_the_gateway(mock, key, tmp_path):
    """The whole point: the chart must outlive the process that drew it."""
    config = make_config(["a"])
    config.store.backend = "sqlite"
    config.store.path = str(tmp_path / "usage.db")

    gateway = build(mock, config)
    await gateway.startup()
    await gateway.complete(body(), {}, key)

    result = await gateway.usage(window="1h")
    assert result["overall"]["requests"] == 1
    assert result["totals"]["a"]["output_tokens"] > 0
    assert result["names"]["a"] == "a", "the legend needs a display name"
    await gateway.aclose()

    # A second gateway over the same file still sees it.
    revived = build(mock, config)
    await revived.startup()
    assert (await revived.usage(window="1h"))["overall"]["requests"] == 1
    await revived.aclose()


async def test_a_failed_request_is_recorded_too(mock, key, tmp_path):
    """A usage chart showing only successes hides the outage you opened it to find.

    Note this covers failures that reached the pipeline. A request rejected during
    validation (no `model`) never touches an account and never becomes gateway
    usage — the event log leaves those out too.
    """
    from support.mock_upstream import auth_error

    from tokenbiryani.core.gateway import GatewayError

    config = make_config(["a"])
    config.store.backend = "sqlite"
    config.store.path = str(tmp_path / "usage.db")
    gateway = build(mock, config)
    await gateway.startup()

    mock.script("a", auth_error(), auth_error(), auth_error())
    with pytest.raises(GatewayError):
        await gateway.complete(body(), {}, key)

    result = await gateway.usage(window="1h")
    assert result["overall"]["requests"] == 1
    assert result["overall"]["errors"] == 1, "an outage must be visible in the chart"
    await gateway.aclose()


def test_sample_from_event_is_json_safe():
    from tokenbiryani.observability.events import RequestEvent

    event = RequestEvent(request_id="r1", started_at=1.0, model="m", key_name="k")
    event.account_id = None          # a request that never reached an account
    event.cost_usd = None
    sample = sample_from_event(event)
    assert sample["account_id"] == ""
    assert sample["cost_usd"] == 0.0
    assert sample["status"] == 0
    assert all(not isinstance(v, (dict, list)) for v in sample.values())


# ---- the bundled price table ---------------------------------------------------
# The standing rule is that the gateway must never bill against a number nobody can
# attribute. A dated file the console shows the date of satisfies it; a dict
# hard-coded in config.py would not, which is why this is data and has an `as_of`.


def test_pricing_builtin_loads_a_dated_table():
    from tokenbiryani.config import Config

    config = Config.from_dict({"pricing": "builtin"})
    assert config.pricing, "the shipped table has models in it"
    assert config.pricing_as_of, "and a date, which the console displays"
    assert config.price_for("claude-opus-5") is not None


def test_an_operator_price_beats_the_bundled_one():
    from tokenbiryani.config import Config

    config = Config.from_dict({"pricing": {
        "builtin": True,
        "claude-opus-5": {"input": 1.0, "output": 2.0},
    }})
    assert config.price_for("claude-opus-5").input == 1.0, "yours wins"
    assert config.price_for("claude-sonnet-5") is not None, "the rest still loads"


def test_prices_stay_absent_unless_asked_for():
    """Opt-in, because a price nobody chose is a number nobody checked."""
    from tokenbiryani.config import Config

    assert Config.from_dict({}).pricing == {}
    assert Config.from_dict({}).pricing_as_of == ""


def test_a_misspelled_pricing_directive_is_refused():
    from tokenbiryani.config import Config, ConfigError

    with pytest.raises(ConfigError) as caught:
        Config.from_dict({"pricing": "buitlin"})
    assert "builtin" in str(caught.value), "the message says what was meant"


def test_the_price_table_ships_with_the_package():
    """A wheel without it makes `pricing: builtin` silently price nothing."""
    import os

    from tokenbiryani.config import BUILTIN_PRICES_PATH

    assert os.path.exists(BUILTIN_PRICES_PATH)
