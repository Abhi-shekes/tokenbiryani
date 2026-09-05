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

## Is it even switched on?

Anthropic's cache engages only where the request carries a `cache_control` marker.
A client that never sets one pays full price on every turn no matter how the gateway
routes, and that looks exactly like a routing failure: cache hit 0%.

The hit rate cannot tell the two apart, so `GET /admin/cache-advice` does. Per virtual
key and model it reports how often a breakpoint was present, how big the stable head
is, the realised hit rate, and a verdict:

| Verdict | Means |
|---|---|
| no `cache_control` breakpoint on a stable prefix of about N tokens | a client problem. **No routing strategy can recover it.** |
| breakpoints are being sent but the hit rate is low | a routing problem — read the rest of this page |
| the stable prefix is too small to cache | nothing to fix |
| caching is engaged and working | nothing to do |

`cache.auto_breakpoint: true` makes the gateway add the marker itself, at the end of
the stable head — the last tool if there are tools, otherwise the system prompt. It
is **off by default and should stay off unless you need it**: everywhere else this
gateway routes rather than rewrites, and turning it on makes it the third exception
to that rule after the two fields Bedrock and Vertex require. It never touches a
request that already has a breakpoint, and never one whose prefix is too short for
Anthropic to cache.

None of this keeps prompt content. The body is read in memory and dropped; what is
recorded is a boolean and a token count.

## What to check

1. **Your strategy.** `sticky_headroom` is the default for this reason. `headroom`,
   `least_loaded` and `round_robin` are all cache-blind — and note that `headroom`
   is `sticky_headroom` with affinity switched off and nothing else changed, so it
   can never route *better*, only the same or worse.
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

Under the default weights that decision is made by the **filter**, not the score.
Affinity is worth 0.40 and headroom at most 0.40, so no headroom advantage a rival
can hold — not even 100% against the owner's 1% — is enough to move a conversation.
What moves it is the owner becoming ineligible: cooling, disabled, over its spend
cap, at max concurrency, or with too little projected headroom to serve the request
at all. Affinity is effectively absolute right up to the point the owner cannot
serve, which is the behaviour you want and is stronger than a scoring trade-off.

Every forced break is counted, so the cost is visible rather than silent.
