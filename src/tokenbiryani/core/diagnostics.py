"""What the rate-limit headers on a real response actually say.

This is the one check no mock-backed test can make: every test in this project runs
against the suite's fake upstream, which encodes an assumption about how
the upstream spells its headers. If the real API spells one differently, that window
stays empty, the account reads as full, and the router silently degrades to
round-robin — shredding the prompt cache while every screen looks healthy.

It lives here, rather than inside `cmd_doctor`, because two callers need the same
answer from the same list: `tokenbiryani doctor` in the terminal, and the console's
verify step. Two copies of the expected-header list is exactly the drift the check
exists to catch.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping

from .limits import UNIFIED_WINDOWS, LimitMirror

#: The three windows the limit mirror models, in the spelling the upstream uses.
LIMIT_WINDOWS = ["requests", "input-tokens", "output-tokens"]

#: ...and the three facts it needs about each of them.
HEADER_TEMPLATES = [
    "anthropic-ratelimit-{}-limit",
    "anthropic-ratelimit-{}-remaining",
    "anthropic-ratelimit-{}-reset",
]

#: A subscription session answers a different question. It never sends the triples
#: above; it sends how much of each rolling window is spent. An account reporting
#: these is not broken, so checking it against the API-key list would report nine
#: faults where there are none — which is the exact false alarm this module exists
#: to prevent, pointed the other way.
UNIFIED_TEMPLATES = [
    "anthropic-ratelimit-unified-{}-status",
    "anthropic-ratelimit-unified-{}-utilization",
    "anthropic-ratelimit-unified-{}-reset",
]


def expected_headers(unified: bool = False) -> List[str]:
    """Every header the router depends on, in the order a report should show them."""
    if unified:
        return [
            template.format(window)
            for window in UNIFIED_WINDOWS
            for template in UNIFIED_TEMPLATES
        ]
    return [
        template.format(window)
        for window in LIMIT_WINDOWS
        for template in HEADER_TEMPLATES
    ]


def looks_unified(lowered: Mapping[str, str]) -> bool:
    """True when the response is a subscription session's, not an API key's."""
    return any(
        str(name).startswith("anthropic-ratelimit-unified-") for name in lowered
    )


def inspect_headers(headers: Mapping[str, str], now: float = 0.0) -> Dict[str, Any]:
    """Compare what arrived against what the mirror looks for, and parse it.

    Returns the whole picture rather than a verdict, because the useful thing to show
    a human is the mismatch itself: which header was expected, what came instead, and
    what the router would therefore believe.
    """
    now = now or time.time()
    lowered = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    unified = looks_unified(lowered)

    checked = []
    missing = []
    for name in expected_headers(unified):
        present = name in lowered
        if not present:
            missing.append(name)
        checked.append({"header": name, "present": present, "value": lowered.get(name)})

    # The real proof is not that the keys exist but that the mirror the router reads
    # parsed something out of them.
    mirror = LimitMirror(observable=not unified)
    mirror.update_from_headers(dict(headers), now)
    snapshot = mirror.snapshot(now)
    if unified:
        parsed = {
            f"unified_{name}": snapshot["unified"][name]
            for name in snapshot["unified"]
        }
    else:
        parsed = {
            label: {
                "limit": snapshot[label]["limit"],
                "remaining": snapshot[label]["remaining"],
                "reset_in": snapshot[label]["reset_in"],
            }
            for label in ("requests", "input_tokens", "output_tokens")
        }

    if not missing:
        consequence = ""
    elif unified:
        consequence = (
            f"{len(missing)} of {len(expected_headers(True))} unified headers are "
            "missing or spelled differently. This account routes on assumed headroom "
            "instead of its real utilisation, so a nearly-exhausted subscription "
            "still reads as half full."
        )
    else:
        consequence = (
            f"{len(missing)} of {len(expected_headers())} headers are missing or "
            "spelled differently. Those windows stay empty, so this account reads "
            "as full and routing degrades to round-robin — which shreds the prompt "
            "cache while every meter looks healthy."
        )

    return {
        "ok": not missing,
        "family": "unified" if unified else "limits",
        "headroom": snapshot["headroom"],
        "checked": checked,
        "missing": missing,
        "returned": {k: v for k, v in sorted(lowered.items()) if k.startswith("anthropic-")},
        "parsed": parsed,
        "consequence": consequence,
    }
