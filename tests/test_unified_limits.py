"""A subscription session reports its budget as utilisation, not as a remaining count.

The gateway used to treat `type: oauth` as "reports nothing", route it on a fixed
guess, and tell the operator its empty meters were expected. They were not: the
response carries `anthropic-ratelimit-unified-*`, which says how much of the rolling
5-hour and 7-day windows is spent. That is a measurement, and a guess must never win
a comparison against one.

What still cannot be done with it: leases and the capacity horizon, both of which
need absolute token counts. A fraction cannot be decremented by 4,000 tokens.
"""

from __future__ import annotations

import pytest

from tokenbiryani.core.diagnostics import expected_headers, inspect_headers, looks_unified
from tokenbiryani.core.limits import LimitMirror, TokenEstimate

NOW = 1_788_600_000.0

#: What a real subscription session returned on 2026-09-05, verbatim.
SUBSCRIPTION_HEADERS = {
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.34",
    "anthropic-ratelimit-unified-5h-reset": "1788614400",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.45",
    "anthropic-ratelimit-unified-7d-reset": "1788667200",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
}


def subscription_mirror() -> LimitMirror:
    mirror = LimitMirror(observable=False, assumed_headroom=0.5)
    mirror.update_from_headers(SUBSCRIPTION_HEADERS, NOW)
    return mirror


def test_utilisation_becomes_headroom():
    mirror = subscription_mirror()
    # The tightest window binds: 7d is 45% spent, so 55% is left.
    assert mirror.headroom(NOW) == 0.55
    assert mirror.unified_known is True


def test_a_measurement_beats_the_assumed_headroom():
    """The whole point: an oauth account no longer routes on a fixed 0.5."""
    guessing = LimitMirror(observable=False, assumed_headroom=0.5)
    measured = subscription_mirror()
    assert guessing.headroom(NOW) == 0.5, "nothing reported: still a guess"
    assert measured.headroom(NOW) != 0.5


def test_a_rejected_window_is_believed_rather_than_proved():
    mirror = LimitMirror(observable=False)
    mirror.update_from_headers(
        {
            "anthropic-ratelimit-unified-5h-status": "rejected",
            "anthropic-ratelimit-unified-5h-utilization": "1.0",
        },
        NOW,
    )
    assert mirror.can_serve(TokenEstimate(10, 10), NOW) is False
    assert mirror.headroom(NOW) == 0.0


def test_a_warning_status_is_not_a_refusal():
    mirror = LimitMirror(observable=False)
    mirror.update_from_headers(
        {
            "anthropic-ratelimit-unified-5h-status": "allowed_warning",
            "anthropic-ratelimit-unified-5h-utilization": "0.9",
        },
        NOW,
    )
    assert mirror.can_serve(TokenEstimate(10, 10), NOW) is True
    assert mirror.headroom(NOW) == pytest.approx(0.1)


def test_the_reset_is_known_so_the_console_can_count_down():
    mirror = subscription_mirror()
    assert mirror.next_reset(NOW) == 14_400.0


def test_an_api_key_account_is_unaffected():
    """Unified parsing must not touch the accounts the router was built for."""
    mirror = LimitMirror()
    mirror.update_from_headers(
        {
            "anthropic-ratelimit-input-tokens-limit": "100",
            "anthropic-ratelimit-input-tokens-remaining": "80",
        },
        NOW,
    )
    assert mirror.headroom(NOW) == 0.8
    assert mirror.unified_known is False
    assert mirror.snapshot(NOW)["unified"]["5h"]["headroom"] is None


def test_whichever_budget_is_tighter_binds():
    """An account reporting both is held to both."""
    mirror = LimitMirror()
    mirror.update_from_headers(
        dict(
            SUBSCRIPTION_HEADERS,
            **{
                "anthropic-ratelimit-input-tokens-limit": "100",
                "anthropic-ratelimit-input-tokens-remaining": "20",
            },
        ),
        NOW,
    )
    assert mirror.headroom(NOW) == 0.2, "the classic window is tighter here"


def test_the_snapshot_carries_both_shapes():
    snapshot = subscription_mirror().snapshot(NOW)
    assert snapshot["unified_known"] is True
    assert snapshot["unified"]["5h"] == {
        "status": "allowed", "utilization": 0.34, "headroom": 0.66, "reset_in": 14400.0,
    }
    # The classic windows are still present and still empty, so one renderer works.
    assert snapshot["input_tokens"]["limit"] is None


# ---- the header check ----------------------------------------------------------

def test_a_subscription_is_checked_against_the_headers_it_actually_sends():
    """Holding it to the API-key list reported nine faults on a healthy account."""
    report = inspect_headers(SUBSCRIPTION_HEADERS, NOW)
    assert report["ok"] is True
    assert report["family"] == "unified"
    assert report["missing"] == []
    assert report["headroom"] == 0.55


def test_a_subscription_missing_a_unified_header_says_what_that_costs():
    report = inspect_headers(
        {"anthropic-ratelimit-unified-5h-status": "allowed"}, NOW
    )
    assert report["ok"] is False
    assert report["family"] == "unified"
    assert "assumed headroom" in report["consequence"]
    assert len(expected_headers(True)) == 6


def test_an_api_key_response_is_still_checked_the_old_way():
    report = inspect_headers({"anthropic-ratelimit-requests-limit": "5"}, NOW)
    assert report["family"] == "limits"
    assert looks_unified({"anthropic-ratelimit-requests-limit": "5"}) is False
    assert "round-robin" in report["consequence"]
