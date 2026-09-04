"""The CLI is the first interface; its renderer is worth a test."""

from __future__ import annotations

from tokenbiryani.cli import build_parser, render_status

SNAPSHOT = {
    "strategy": "sticky_headroom",
    "pool": {
        "total": 2,
        "ready": 1,
        "cooling": 1,
        "disabled": 0,
        "ready_input_tokens": 702_000,
        "next_reset_seconds": 12.0,
    },
    "queue": {"depth": 0, "max_size": 128, "highest_priority": None},
    "stats": {
        "requests": 412,
        "failovers": 3,
        "cache_breaks": 1,
        "errors": 0,
        "cache_hit_rate": 0.94,
        "spend_usd": 18.4,
    },
    "accounts": [
        {
            "id": "acct-01",
            "state": "ready",
            "disabled_reason": None,
            "cooling_for": None,
            "cache_hit_rate": 0.97,
            "limits": {
                "requests": {"fraction": 0.98, "reset_in": 41},
                "input_tokens": {"fraction": 0.82, "reset_in": 41},
                "output_tokens": {"fraction": 0.79, "reset_in": 41},
            },
        },
        {
            "id": "acct-03",
            "state": "cooling",
            "disabled_reason": None,
            "cooling_for": 27.0,
            "cache_hit_rate": None,
            "limits": {
                "requests": {"fraction": 0.04, "reset_in": 27},
                "input_tokens": {"fraction": 0.0, "reset_in": 27},
                "output_tokens": {"fraction": 0.06, "reset_in": 27},
            },
        },
    ],
}


def test_status_renders_without_color():
    output = render_status(SNAPSHOT, color=False)
    assert "702k tok ready" in output
    assert "00:12" in output
    assert "acct-01" in output and "ready" in output
    assert "acct-03" in output and "cooling" in output
    assert "94%" in output
    assert "\033[" not in output, "NO_COLOR output must contain no escape sequences"


def test_status_colors_states_when_enabled():
    output = render_status(SNAPSHOT, color=True)
    assert "\033[" in output


def test_missing_cache_rate_renders_as_a_dash():
    assert "—" in render_status(SNAPSHOT, color=False)


def test_parser_has_the_four_commands():
    parser = build_parser()
    for command in ("init", "serve", "status", "keygen"):
        assert parser.parse_args([command]).command == command


def test_short_keys_are_masked_completely():
    from tokenbiryani.core.keys import generate_key, mask_key

    assert mask_key("bir_test") == "•" * 8
    assert "bir_test" not in mask_key("bir_test")
    long_key = generate_key()
    masked = mask_key(long_key)
    assert masked.endswith("…")
    assert len(masked) < len(long_key)
    assert long_key not in masked
