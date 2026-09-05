"""Pacing: spending a quota window on purpose rather than by accident."""

from __future__ import annotations

import datetime as _dt
import time

from tokenbiryani.core.pacing import (
    MODE_ENFORCING,
    PacingGovernor,
    business_hours_fraction,
    week_start,
)
from tokenbiryani.core.queue import PRIORITY_BATCH, PRIORITY_INTERACTIVE

WEEK = 7 * 24 * 3600.0


def at(day: str, hour: int = 12) -> float:
    """A timestamp on a named weekday of a known week, in UTC."""
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    monday = _dt.datetime(2026, 3, 2, tzinfo=_dt.timezone.utc)  # a Monday
    return (monday + _dt.timedelta(days=days.index(day), hours=hour)).timestamp()


def governor(**kwargs) -> PacingGovernor:
    return PacingGovernor(**kwargs)


# ---- the week --------------------------------------------------------------------


def test_the_week_starts_on_monday_in_utc():
    start = week_start(at("thursday", 15))
    assert _dt.datetime.fromtimestamp(start, tz=_dt.timezone.utc).weekday() == 0
    assert _dt.datetime.fromtimestamp(start, tz=_dt.timezone.utc).hour == 0


def test_monday_morning_is_the_start_of_its_own_week():
    assert week_start(at("monday", 1)) == at("monday", 0)


# ---- the business-hours curve ----------------------------------------------------


def test_the_weekend_does_not_advance_the_business_curve():
    """A linear target expects a fifth of the quota spent over a weekend, so a
    Monday-to-Friday team reads as behind pace every Monday morning."""
    assert business_hours_fraction(at("saturday", 12)) == 1.0
    assert business_hours_fraction(at("friday", 23)) == 1.0


def test_the_business_curve_advances_through_a_working_day():
    before = business_hours_fraction(at("tuesday", 9))
    after = business_hours_fraction(at("tuesday", 17))
    assert after > before
    assert 0.0 < before < after < 1.0


def test_nothing_has_elapsed_before_monday_opening():
    assert business_hours_fraction(at("monday", 8)) == 0.0


# ---- readings from a subscription window -----------------------------------------


def test_spending_evenly_reads_as_on_pace():
    now = time.time()
    reading = governor().from_unified("acct-01", 0.5, WEEK / 2, now)
    assert reading is not None
    assert abs(reading.pace) < 0.01
    assert reading.verdict == "on pace"


def test_burning_the_week_early_is_named_with_a_date():
    now = time.time()
    # Half the window gone, 90% of the quota spent.
    reading = governor().from_unified("acct-01", 0.9, WEEK / 2, now)
    assert reading.pace > 0.3
    assert reading.exhausted_in_seconds is not None
    assert "ahead of pace" in reading.verdict
    assert "quota is gone in" in reading.verdict


def test_stranding_quota_is_named_with_the_fraction():
    now = time.time()
    # Three quarters through the window, a fifth of the quota used.
    reading = governor().from_unified("acct-01", 0.2, WEEK / 4, now)
    assert reading.pace < -0.3
    assert reading.stranded_fraction > 0.5
    assert "behind pace" in reading.verdict
    assert "unused" in reading.verdict


def test_an_account_reporting_no_reset_is_not_paced():
    """No reset means no window, and a pace against no window is invented."""
    assert governor().from_unified("acct-01", 0.5, None, time.time()) is None


def test_the_first_moment_of_a_window_predicts_nothing():
    """Extrapolating from zero elapsed time turns one request into 'you run dry
    today'."""
    reading = governor().from_unified("acct-01", 0.0, WEEK, time.time())
    assert reading.exhausted_in_seconds is None
    assert reading.projected_utilization == 0.0


# ---- readings from a budget ------------------------------------------------------


def test_an_api_key_pool_is_not_paced_without_a_budget():
    """No header says anything about a week, so without a stated budget there is
    nothing to pace against and inventing one would be a number nobody can
    attribute."""
    assert governor().from_budget("pool", 100.0, time.time()) is None


def test_a_stated_budget_gives_a_pace():
    now = at("thursday", 12)  # ~3.5/7 of the week
    reading = governor(weekly_budget_usd=100.0).from_budget("pool", 90.0, now)
    assert reading is not None
    assert reading.utilization == 0.9
    assert reading.pace > 0.3
    assert "ahead of pace" in reading.verdict


def test_a_budget_reading_is_always_the_calendar_week():
    reading = governor(window="5h", weekly_budget_usd=100.0).from_budget(
        "pool", 10.0, at("wednesday")
    )
    assert reading.window == "7d"


# ---- enforcement -----------------------------------------------------------------


def test_advisory_mode_never_delays_anything():
    gov = governor(mode="advisory")
    assert gov.delay_for(PRIORITY_BATCH, PRIORITY_BATCH, pace=0.9) == 0.0


def test_interactive_traffic_is_never_delayed():
    """No threshold makes slowing someone's session the right way to hit a budget."""
    gov = governor(mode=MODE_ENFORCING)
    assert gov.delay_for(PRIORITY_INTERACTIVE, PRIORITY_BATCH, pace=0.9) == 0.0


def test_batch_traffic_is_held_back_when_ahead_of_pace():
    gov = governor(mode=MODE_ENFORCING, ahead_threshold=0.1, max_batch_delay_seconds=30)
    assert gov.delay_for(PRIORITY_BATCH, PRIORITY_BATCH, pace=0.3) == 30.0


def test_the_delay_scales_with_how_far_ahead_it_is():
    gov = governor(mode=MODE_ENFORCING, ahead_threshold=0.2, max_batch_delay_seconds=40)
    small = gov.delay_for(PRIORITY_BATCH, PRIORITY_BATCH, pace=0.25)
    large = gov.delay_for(PRIORITY_BATCH, PRIORITY_BATCH, pace=0.35)
    assert 0 < small < large <= 40


def test_being_on_pace_delays_nothing():
    gov = governor(mode=MODE_ENFORCING, ahead_threshold=0.1)
    assert gov.delay_for(PRIORITY_BATCH, PRIORITY_BATCH, pace=0.05) == 0.0
    assert gov.delay_for(PRIORITY_BATCH, PRIORITY_BATCH, pace=-0.5) == 0.0


def test_no_pace_at_all_delays_nothing():
    gov = governor(mode=MODE_ENFORCING)
    assert gov.delay_for(PRIORITY_BATCH, PRIORITY_BATCH, pace=None) == 0.0


# ---- through the gateway ---------------------------------------------------------


async def test_a_pool_with_nothing_to_pace_reports_nothing(gateway_factory):
    """API keys report no weekly window, and no budget was stated."""
    gateway = gateway_factory(["acct-01"])
    report = await gateway.pacing_report()
    assert report["readings"] == []
    assert report["mode"] == "advisory"


async def test_a_budget_gives_the_pool_a_reading(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"],
        overrides={"pacing": {"weekly_budget_usd": 100.0}, "pricing": {
            "claude-test-1": {"input": 3.0, "output": 15.0}
        }},
    )
    from conftest import body

    await gateway.complete(body(), {}, key)
    report = await gateway.pacing_report()
    assert len(report["readings"]) == 1
    assert report["readings"][0]["scope"] == "pool"
    assert report["readings"][0]["source"] == "budget"


async def test_disabling_pacing_reports_nothing(gateway_factory):
    gateway = gateway_factory(
        ["acct-01"],
        overrides={"pacing": {"enabled": False, "weekly_budget_usd": 100.0}},
    )
    assert (await gateway.pacing_report())["readings"] == []


async def test_interactive_requests_are_not_paced_even_when_enforcing(
    gateway_factory, key
):
    gateway = gateway_factory(
        ["acct-01"],
        overrides={"pacing": {
            "mode": "enforcing",
            "weekly_budget_usd": 0.000001,
            "max_batch_delay_seconds": 5,
        }},
    )
    from conftest import body

    started = time.time()
    await gateway.complete(body(), {}, key)
    assert time.time() - started < 1.0
    assert gateway.events.recent(1)[0]["paced_for"] == 0.0


async def test_batch_requests_are_held_back_when_the_pool_is_ahead(
    gateway_factory, key
):
    """The enforcing path end to end: a wait, not a rejection. Waiting is what
    spends a window more slowly — rejecting just moves the same work to whenever
    the client retries, which is usually immediately."""
    gateway = gateway_factory(
        ["acct-01"],
        overrides={
            "pacing": {
                "mode": "enforcing",
                "weekly_budget_usd": 0.000001,
                "ahead_threshold": 0.1,
                "max_batch_delay_seconds": 0.3,
            },
            "pricing": {"claude-test-1": {"input": 3.0, "output": 15.0}},
        },
    )
    from conftest import body

    # One request to put spend on the ledger, so there is a pace to be ahead of.
    await gateway.complete(body(), {}, key)

    await gateway.complete(body(), {"X-TokenBiryani-Priority": "batch"}, key)
    assert gateway.events.recent(1)[0]["paced_for"] > 0.0


async def test_a_subscription_window_paces_its_own_account(gateway_factory):
    """The good path: the account reports its own utilisation and reset, so the
    pace is measured rather than declared."""
    gateway = gateway_factory(["acct-01"])
    account = gateway.accounts["acct-01"]
    reset = _dt.datetime.fromtimestamp(
        time.time() + WEEK / 2, tz=_dt.timezone.utc
    ).isoformat()
    account.mirror.update_from_headers(
        {
            "anthropic-ratelimit-unified-7d-status": "allowed_warning",
            "anthropic-ratelimit-unified-7d-utilization": "0.9",
            "anthropic-ratelimit-unified-7d-reset": reset,
        },
        time.time(),
    )

    readings = (await gateway.pacing_report())["readings"]
    assert len(readings) == 1
    assert readings[0]["scope"] == "acct-01"
    assert readings[0]["source"] == "unified"
    assert "ahead of pace" in readings[0]["verdict"]


# ---- pace-aware policy -----------------------------------------------------------

AHEAD = {
    "mode": "enforcing",
    "weekly_budget_usd": 0.000001,
    "ahead_threshold": 0.1,
    "max_batch_delay_seconds": 0.05,
}
PRICED = {"claude-test-1": {"input": 3.0, "output": 15.0}}


async def _get_ahead(gateway, key):
    """One priced request, so there is spend on the ledger to be ahead of."""
    from conftest import body as _body

    await gateway.complete(_body(), {}, key)


async def test_a_downshift_needs_to_be_configured(gateway_factory, key):
    """Off unless asked for: it is the one lever that changes the answer."""
    gateway = gateway_factory(
        ["acct-01"], overrides={"pacing": AHEAD, "pricing": PRICED}
    )
    await _get_ahead(gateway, key)

    from conftest import body as _body

    await gateway.complete(_body(), {"X-TokenBiryani-Priority": "batch"}, key)
    assert gateway.events.recent(1)[0]["model_requested"] == ""


async def test_batch_work_downshifts_when_ahead_of_pace(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"],
        overrides={
            "pacing": dict(AHEAD, model_downshift={"claude-test-1": "claude-test-2"}),
            "pricing": PRICED,
        },
    )
    await _get_ahead(gateway, key)

    from conftest import body as _body

    await gateway.complete(_body(), {"X-TokenBiryani-Priority": "batch"}, key)
    event = gateway.events.recent(1)[0]
    assert event["model"] == "claude-test-2"
    assert event["model_requested"] == "claude-test-1"


async def test_interactive_work_is_never_downshifted(gateway_factory, key):
    """Batch-priority only. Substituting a model on someone's live session changes
    the answer they are reading."""
    gateway = gateway_factory(
        ["acct-01"],
        overrides={
            "pacing": dict(AHEAD, model_downshift={"claude-test-1": "claude-test-2"}),
            "pricing": PRICED,
        },
    )
    await _get_ahead(gateway, key)

    from conftest import body as _body

    await gateway.complete(_body(), {}, key)
    event = gateway.events.recent(1)[0]
    assert event["model"] == "claude-test-1"
    assert event["model_requested"] == ""


async def test_a_downshift_cannot_widen_what_a_key_may_reach(gateway_factory):
    """A pacing policy must not hand a tenant a model their key does not allow."""
    from tokenbiryani.config import KeyConfig

    gateway = gateway_factory(
        ["acct-01"],
        overrides={
            "pacing": dict(AHEAD, model_downshift={"claude-test-1": "claude-test-2"}),
            "pricing": PRICED,
        },
    )
    narrow = KeyConfig(key="bir_test", name="default", models=["claude-test-1"])
    await _get_ahead(gateway, narrow)

    from conftest import body as _body

    await gateway.complete(_body(), {"X-TokenBiryani-Priority": "batch"}, narrow)
    assert gateway.events.recent(1)[0]["model"] == "claude-test-1"


async def test_nothing_is_downshifted_while_on_pace(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"],
        overrides={
            "pacing": {
                "mode": "enforcing",
                "weekly_budget_usd": 1_000_000.0,
                "model_downshift": {"claude-test-1": "claude-test-2"},
            },
            "pricing": PRICED,
        },
    )
    await _get_ahead(gateway, key)

    from conftest import body as _body

    await gateway.complete(_body(), {"X-TokenBiryani-Priority": "batch"}, key)
    assert gateway.events.recent(1)[0]["model"] == "claude-test-1"


async def test_advisory_mode_changes_nothing(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"],
        overrides={
            "pacing": {
                "mode": "advisory",
                "weekly_budget_usd": 0.000001,
                "model_downshift": {"claude-test-1": "claude-test-2"},
            },
            "pricing": PRICED,
        },
    )
    await _get_ahead(gateway, key)

    from conftest import body as _body

    await gateway.complete(_body(), {"X-TokenBiryani-Priority": "batch"}, key)
    event = gateway.events.recent(1)[0]
    assert event["model"] == "claude-test-1"
    assert event["paced_for"] == 0.0


async def test_batch_work_prefers_the_cheaper_lane_when_ahead(gateway_factory, key):
    """The pool has capacity — it spills anyway, because the Batches API is priced
    below standard and spends a different upstream limit. Cheaper than waiting, so
    it is tried before the delay."""
    gateway = gateway_factory(
        ["acct-01"],
        overrides={
            "pacing": dict(AHEAD, prefer_batch_lane_when_ahead=True),
            "batch": {"enabled": True, "poll_interval_seconds": 0.01},
            "pricing": PRICED,
        },
    )
    await _get_ahead(gateway, key)

    from conftest import body as _body

    completion = await gateway.complete(
        _body(), {"X-TokenBiryani-Priority": "batch"}, key
    )
    assert completion.headers["x-tokenbiryani-via"] == "batch"
    assert gateway.events.recent(1)[0]["via"] == "batch"


async def test_the_lane_preference_can_be_turned_off(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"],
        overrides={
            "pacing": dict(AHEAD, prefer_batch_lane_when_ahead=False),
            "batch": {"enabled": True, "poll_interval_seconds": 0.01},
            "pricing": PRICED,
        },
    )
    await _get_ahead(gateway, key)

    from conftest import body as _body

    completion = await gateway.complete(
        _body(), {"X-TokenBiryani-Priority": "batch"}, key
    )
    assert completion.headers.get("x-tokenbiryani-via") != "batch"
    assert gateway.events.recent(1)[0]["paced_for"] > 0.0


async def test_interactive_work_never_takes_the_batch_lane(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"],
        overrides={
            "pacing": AHEAD,
            "batch": {"enabled": True, "poll_interval_seconds": 0.01},
            "pricing": PRICED,
        },
    )
    await _get_ahead(gateway, key)

    from conftest import body as _body

    completion = await gateway.complete(_body(), {}, key)
    assert completion.headers.get("x-tokenbiryani-via") != "batch"


async def test_without_a_batch_lane_there_is_nothing_to_prefer(gateway_factory, key):
    """`batch.enabled` is off, so the policy falls through to the delay."""
    gateway = gateway_factory(
        ["acct-01"], overrides={"pacing": AHEAD, "pricing": PRICED}
    )
    await _get_ahead(gateway, key)

    from conftest import body as _body

    await gateway.complete(_body(), {"X-TokenBiryani-Priority": "batch"}, key)
    assert gateway.events.recent(1)[0]["paced_for"] > 0.0
