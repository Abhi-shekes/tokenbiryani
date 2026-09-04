# Why is my bill higher?

Almost always: the prompt cache stopped working, and the usual cause is a routing
strategy that ignores it.

## The mechanism

Anthropic's prompt cache is scoped **per credential**. An agent conversation resends a
large stable prefix — system prompt, tool definitions, history — on every turn. Warm,
those tokens bill at roughly a tenth of the input rate.

Spread those turns across accounts and each one is a cache miss on an account that has
never seen the prefix. The work is identical; the bill is not.

## What it costs

24 concurrent conversations, 8 turns each, across 4 accounts, against an upstream that
models the per-credential cache:

| Strategy | Cache hit | Cache breaks | Cost | vs sticky |
|---|---|---|---|---|
| `sticky_headroom` | 78.6% | 0 | $0.4923 | — |
| `round_robin` | 47.8% | 144 | $0.9535 | 1.94x |
| `least_loaded` | 47.8% | 144 | $0.9535 | 1.94x |
| `headroom` | 47.8% | 144 | $0.9535 | 1.94x |

Reproduce with `python benchmarks/cache_affinity.py`. Prices there are illustrative
ratios, not a price list.

Note that all three cache-blind strategies pay **the same** penalty. Any strategy that
ignores affinity visits every account once per conversation, so they take the same
number of misses. The penalty is inherent to cache-blindness, not a quirk of
round-robin.

## What to check

1. **Your strategy.** `sticky_headroom` is the default for this reason. `headroom`,
   `least_loaded` and `round_robin` are all cache-blind.
2. **Cache breaks.** `tokenbiryani status` reports them, and so does the console.
   A healthy pool should show approximately zero. Breaks mean the affinity owner
   could not serve — usually because it was cooling.
3. **Per-account cache hit rate.** In the console, an account near 0% is being handed
   new conversations rather than continuations. That is expected for a spillover
   account and a problem for your primary.
4. **Affinity TTL.** `routing.affinity_ttl_seconds` defaults to 1800. A conversation
   idle for longer loses its owner, which is correct — the upstream cache has expired
   by then too.

## When breaking affinity is right

When the owner genuinely cannot serve: it is cooling, disabled, or out of headroom.
The router scores affinity against real availability rather than treating it as
absolute, and every forced break is counted so the cost is visible rather than
silent.
