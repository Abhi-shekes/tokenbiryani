#!/usr/bin/env python3
"""Does sticky routing actually pay for itself?

PLAN.md names cache fragmentation as the project's highest risk: a gateway that
balances load without cache awareness can cost *more* than no gateway at all. This
benchmark is the evidence for or against that claim, and for the README's assertion
that `round_robin` ships to be beaten.

It runs the same workload — concurrent multi-turn conversations against a pool —
under each strategy, against a mock upstream that models Anthropic's per-credential
prompt cache. Everything measured is real behaviour of the real router; only the
upstream is simulated.

    python benchmarks/cache_affinity.py
    python benchmarks/cache_affinity.py --conversations 40 --turns 10 --markdown

The prices below are RATIOS, not Anthropic's list. The gateway ships no price list
(see README), and what matters here is the shape: a cache read costs roughly a tenth
of a fresh input token, and a cache write a little more than one.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from typing import Any, Dict, List

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(_ROOT, "src"))
# The fake upstream is test scaffolding and no longer ships inside the package, so
# this reaches it where it lives rather than where tokenbiryani is installed.
sys.path.insert(0, os.path.join(_ROOT, "tests"))

from support.mock_upstream import MockAnthropic  # noqa: E402

from tokenbiryani.config import Config, KeyConfig  # noqa: E402
from tokenbiryani.core.gateway import Gateway  # noqa: E402

BASE_URL = "https://mock.anthropic.test"

#: Illustrative ratios per million tokens, not a price list.
PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}

#: A Claude-Code-shaped request: a large stable head, a short turn.
SYSTEM_PROMPT = "You are a careful software engineering assistant. " * 120
TOOLS = [
    {"name": "read_file", "description": "Read a file from disk. " * 20},
    {"name": "write_file", "description": "Write a file to disk. " * 20},
    {"name": "run_command", "description": "Run a shell command. " * 20},
]

STRATEGIES = ["sticky_headroom", "round_robin", "least_loaded", "headroom"]


def make_config(accounts: int, strategy: str) -> Config:
    return Config.from_dict({
        "routing": {"strategy": strategy},
        "retry": {"backoff_base_seconds": 0.0, "backoff_max_seconds": 0.0},
        "accounts": [
            {"id": f"acct-{i + 1:02d}", "type": "anthropic_api",
             "api_key": f"key-{i + 1:02d}", "base_url": BASE_URL}
            for i in range(accounts)
        ],
        "keys": [{"key": "bir_bench", "name": "bench", "admin": True}],
        "pricing": {"claude-bench": PRICING},
    })


def turn_body(conversation: int, turn: int) -> Dict[str, Any]:
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": f"Task {conversation}: refactor and explain."}
    ]
    for index in range(turn):
        messages.append({"role": "assistant", "content": f"Step {index} done."})
        messages.append({"role": "user", "content": f"Continue with step {index + 1}."})
    return {
        "model": "claude-bench",
        "max_tokens": 512,
        "system": SYSTEM_PROMPT,
        "tools": TOOLS,
        "messages": messages,
    }


async def run_strategy(
    strategy: str, accounts: int, conversations: int, turns: int
) -> Dict[str, Any]:
    mock = MockAnthropic()
    for index in range(accounts):
        mock.add(f"acct-{index + 1:02d}", f"key-{index + 1:02d}", cache_aware=True)

    config = make_config(accounts, strategy)
    gateway = Gateway(config, client=mock.client(base_url=BASE_URL))
    await gateway.startup()
    key = KeyConfig(key="bir_bench", name="bench", admin=True)

    async def conversation(index: int) -> None:
        for turn in range(turns):
            await gateway.complete(turn_body(index, turn), {}, key)

    started = time.time()
    await asyncio.gather(*[conversation(i) for i in range(conversations)])
    elapsed = time.time() - started

    events = gateway.events.recent(conversations * turns + 10)
    cache_read = sum(e["cache_read_tokens"] for e in events)
    cache_write = sum(e["cache_creation_tokens"] for e in events)
    fresh_input = sum(e["input_tokens"] for e in events)
    billed_input = cache_read + cache_write + fresh_input
    cost = sum(e["cost_usd"] or 0.0 for e in events)
    breaks = sum(1 for e in events if e["affinity_broken"])
    latencies = [e["latency"] for e in events if e["latency"]]
    await gateway.aclose()

    return {
        "strategy": strategy,
        "requests": len(events),
        "cache_hit_rate": (cache_read / billed_input) if billed_input else 0.0,
        "cache_breaks": breaks,
        "cost_usd": cost,
        "billed_input": billed_input,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "p50_latency": statistics.median(latencies) if latencies else 0.0,
        "wall_seconds": elapsed,
    }


def render(results: List[Dict[str, Any]], markdown: bool) -> str:
    baseline = results[0]["cost_usd"] or 1.0
    header = ["Strategy", "Cache hit", "Cache breaks", "Cost", "vs sticky", "Billed input"]
    rows = []
    for row in results:
        multiple = (row["cost_usd"] / baseline) if baseline else 0.0
        rows.append([
            row["strategy"],
            "{:.1%}".format(row["cache_hit_rate"]),
            str(row["cache_breaks"]),
            "${:.4f}".format(row["cost_usd"]),
            "—" if row is results[0] else f"{multiple:.2f}x",
            "{:,}".format(row["billed_input"]),
        ])

    if markdown:
        out = ["| " + " | ".join(header) + " |",
               "|" + "|".join(["---"] * len(header)) + "|"]
        out += ["| " + " | ".join(r) + " |" for r in rows]
        return "\n".join(out)

    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    line = "  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(header))
    out = [line, "  " + "  ".join("-" * w for w in widths)]
    out += ["  " + "  ".join(r[i].ljust(widths[i]) for i in range(len(header))) for r in rows]
    return "\n".join(out)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--accounts", type=int, default=4)
    parser.add_argument("--conversations", type=int, default=24)
    parser.add_argument("--turns", type=int, default=8)
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--strategies", default=",".join(STRATEGIES))
    args = parser.parse_args()

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    if not args.markdown:
        print()
        print(
            f"  {args.conversations} conversations x {args.turns} turns "
            f"across {args.accounts} accounts"
        )
        print("  prices are illustrative ratios, not a price list")
        print()

    results = []
    for strategy in strategies:
        results.append(await run_strategy(
            strategy, args.accounts, args.conversations, args.turns))

    print(render(results, args.markdown))
    if not args.markdown:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.get_event_loop().run_until_complete(main()))
