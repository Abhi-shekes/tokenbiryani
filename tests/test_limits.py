"""Mirror, estimation, and leases — the mechanism that stops a concurrency stampede."""

from __future__ import annotations

import time

from tokenbiryani.core.limits import (
    LimitMirror,
    TokenEstimate,
    estimate_request,
    parse_reset,
)


def headers(remaining_input=50_000, limit_input=100_000, reset="2030-01-01T00:00:00Z"):
    return {
        "anthropic-ratelimit-requests-limit": "1000",
        "anthropic-ratelimit-requests-remaining": "900",
        "anthropic-ratelimit-requests-reset": reset,
        "anthropic-ratelimit-input-tokens-limit": str(limit_input),
        "anthropic-ratelimit-input-tokens-remaining": str(remaining_input),
        "anthropic-ratelimit-input-tokens-reset": reset,
        "anthropic-ratelimit-output-tokens-limit": "20000",
        "anthropic-ratelimit-output-tokens-remaining": "18000",
        "anthropic-ratelimit-output-tokens-reset": reset,
    }


def test_parse_reset_handles_trailing_z():
    assert parse_reset("2030-01-01T00:00:00Z") == parse_reset("2030-01-01T00:00:00+00:00")
    assert parse_reset(None) is None
    assert parse_reset("garbage") is None


def test_unknown_windows_read_as_full():
    mirror = LimitMirror()
    now = time.time()
    assert mirror.headroom(now) == 1.0
    assert mirror.can_serve(TokenEstimate(1_000_000, 1000), now)


def test_headroom_tracks_the_binding_dimension():
    mirror = LimitMirror()
    now = time.time()
    mirror.update_from_headers(headers(remaining_input=20_000), now)
    # input is the tightest at 0.2; requests 0.9, output 0.9
    assert abs(mirror.headroom(now) - 0.2) < 1e-9


def test_leases_reduce_projected_headroom():
    mirror = LimitMirror()
    now = time.time()
    mirror.update_from_headers(headers(remaining_input=10_000), now)
    lease = mirror.reserve(TokenEstimate(input_tokens=6_000, output_tokens=100), "acct")
    assert mirror.input_tokens.available(mirror.reserved_input, now) == 4_000
    assert not mirror.can_serve(TokenEstimate(5_000, 100), now)
    mirror.release(lease)
    assert mirror.can_serve(TokenEstimate(5_000, 100), now)


def test_release_is_idempotent():
    mirror = LimitMirror()
    lease = mirror.reserve(TokenEstimate(100, 10), "acct")
    mirror.release(lease)
    mirror.release(lease)
    assert mirror.reserved_input == 0


def test_a_passed_reset_refills_the_window():
    mirror = LimitMirror()
    now = time.time()
    mirror.update_from_headers(headers(remaining_input=0, reset="2020-01-01T00:00:00Z"), now)
    assert mirror.input_tokens.available(0, now) == 100_000
    assert mirror.headroom(now) == 1.0


def test_exceeds_capacity_fails_fast():
    mirror = LimitMirror()
    mirror.update_from_headers(headers(), time.time())
    assert mirror.exceeds_capacity(TokenEstimate(500_000, 100))
    assert not mirror.exceeds_capacity(TokenEstimate(50_000, 100))


def test_estimate_uses_max_tokens_for_output():
    estimate = estimate_request({"messages": [{"role": "user", "content": "x" * 350}],
                                 "max_tokens": 777})
    assert estimate.output_tokens == 777
    assert estimate.input_tokens > 50


def test_estimate_is_pessimistic_with_margin():
    small = estimate_request({"messages": []}, safety_margin=1.0)
    large = estimate_request({"messages": []}, safety_margin=2.0)
    assert large.input_tokens > small.input_tokens
