"""The benchmark backs the project's central claim, so it gets a regression guard.

If sticky routing ever stops beating cache-blind routing, the README is wrong and
this fails — which is the point.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmarks")
)

from cache_affinity import render, run_strategy  # noqa: E402

SMALL = dict(accounts=3, conversations=4, turns=6)


async def test_sticky_beats_cache_blind_routing():
    sticky = await run_strategy("sticky_headroom", **SMALL)
    blind = await run_strategy("round_robin", **SMALL)

    assert sticky["cache_hit_rate"] > blind["cache_hit_rate"]
    assert sticky["cost_usd"] < blind["cost_usd"]
    assert sticky["cache_breaks"] == 0, "affinity must hold on a healthy pool"
    assert blind["cache_breaks"] > 0, "round_robin exists to be beaten"


async def test_the_same_work_is_sent_either_way():
    """The cost difference is cache economics, not extra tokens."""
    sticky = await run_strategy("sticky_headroom", **SMALL)
    blind = await run_strategy("round_robin", **SMALL)
    assert sticky["billed_input"] == blind["billed_input"]


async def test_every_cache_blind_strategy_pays_the_same_penalty():
    """Any strategy that ignores affinity visits every account once per conversation,
    so they all take the same number of misses. The penalty is inherent, not a quirk
    of round-robin."""
    results = [await run_strategy(s, **SMALL)
               for s in ("round_robin", "least_loaded", "headroom")]
    hit_rates = {round(r["cache_hit_rate"], 6) for r in results}
    assert len(hit_rates) == 1, hit_rates


async def test_all_requests_succeed():
    result = await run_strategy("sticky_headroom", **SMALL)
    assert result["requests"] == SMALL["conversations"] * SMALL["turns"]


def test_render_produces_a_markdown_table():
    rows = [
        {"strategy": "sticky_headroom", "cache_hit_rate": 0.8, "cache_breaks": 0,
         "cost_usd": 0.47, "billed_input": 1000},
        {"strategy": "round_robin", "cache_hit_rate": 0.48, "cache_breaks": 144,
         "cost_usd": 0.95, "billed_input": 1000},
    ]
    table = render(rows, markdown=True)
    assert table.startswith("| Strategy |")
    assert "2.02x" in table or "2.02" in table
    assert "80.0%" in table


@pytest.mark.parametrize("markdown", [True, False])
def test_render_never_crashes_on_a_single_row(markdown):
    render([{"strategy": "sticky_headroom", "cache_hit_rate": 0.0, "cache_breaks": 0,
             "cost_usd": 0.0, "billed_input": 0}], markdown=markdown)
