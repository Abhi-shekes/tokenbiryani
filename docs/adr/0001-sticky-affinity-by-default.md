# 1. Sticky affinity is the default, not load balancing

**Status:** accepted

## Context

Anthropic's prompt cache is scoped per credential. A Claude Code conversation resends a
large stable prefix — system prompt, tool definitions, history — every turn. Warm, those
tokens bill at roughly a tenth of the input rate.

The obvious behaviour for a pooling gateway is to balance load across accounts. For this
workload that is actively harmful: round-robin turns nearly every turn into a cache miss.

## Decision

The default strategy is `sticky_headroom`: affinity first, rebalance only when the owner
cannot serve the request. Breaking affinity is scored as a cost, not treated as free.

Session identity comes from an explicit `X-TokenBiryani-Session` header, or a fingerprint
of the request's stable head (system + tools + first two messages) so it works with
unmodified clients and stays constant as a conversation grows.

Every forced break emits an event and increments `tokenbiryani_cache_breaks_total`.

## Consequences

- Load is deliberately uneven. `headroom` exists for callers who genuinely want spread.
- Cache hit rate is a first-class metric; a regression here is a cost regression.
- `round_robin` ships as a baseline so the default can be benchmarked, not just asserted.
