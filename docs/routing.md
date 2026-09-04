# Routing

Every request is filtered, scored and picked, and every candidate's reasoning is kept
so `/admin/requests/{id}` can show why the losers lost.

## The availability signal

Every Anthropic response carries the account's live budget:

```
anthropic-ratelimit-requests-limit / -remaining / -reset
anthropic-ratelimit-input-tokens-limit / -remaining / -reset
anthropic-ratelimit-output-tokens-limit / -remaining / -reset
retry-after                                            (on 429)
```

The gateway mirrors these per account on **every** response, so availability is
real-time with no probe traffic. A window whose reset has passed reads as refilled; a
window never seen reads as full, which is the right optimism for a first request.

## Filtering

An account is eligible if it is healthy, in the key's pool scope, supports the model,
is under its spend cap, is below `max_concurrency`, has a closed circuit breaker, and
has projected headroom for the request. Anything else produces a verdict string that
travels to the inspector: `filtered — cooling, 27s remaining` is a complete answer.

## Scoring

```
score =  w_affinity  · is_sticky_owner
       + w_headroom  · min(token_headroom, request_headroom)
       + w_priority  · account_priority
       - w_load      · inflight / max_concurrency
       - w_errors    · recent_error_rate
       - w_cost      · normalized_cost_per_tier
```

Weights are `routing.weights` in the config. Ties break on a deterministic hash of the
session key, so selection is stable and reproducible under replay.

## Strategies

| Strategy | Behaviour |
|---|---|
| `sticky_headroom` | **Default.** Affinity, then most headroom. |
| `headroom` | Pure most-available. Correct for stateless batch traffic. |
| `cost_tiered` | Drain cheap accounts first, spill upward. |
| `priority` | Strict ordered failover: primary, then backup. |
| `least_loaded` | Baseline. |
| `round_robin` | Baseline. Ignores every signal on purpose. |

The last three are cache-blind. Read [Why is my bill higher?](caching.md) before
choosing one.

`tokenbiryani strategies` lists what your install actually has, including plugins.

## Leases

Scoring alone is not enough. Without an atomic reservation, N concurrent requests all
read the same "plenty of headroom" and stampede one account into a 429.

So the estimated cost is **reserved before dispatch** and released on response, then
reconciled against actual usage. Estimates are deliberately pessimistic
(`routing.estimate_safety_margin`, default 1.15): reserving slightly too much is the
safe direction.

## Session affinity

The affinity key is the `X-TokenBiryani-Session` header when present, otherwise a
fingerprint of the request's stable head — system prompt, tool names, and the first
two messages. That stays constant as a conversation grows, so no client changes are
needed.
