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
| `headroom` | `sticky_headroom` with affinity off. Identical to it whenever a request has no cache owner, so it never routes better — only the same, or worse. |
| `cost_tiered` | Drain cheap accounts first, spill upward. |
| `priority` | Strict ordered failover: primary, then backup. |
| `least_loaded` | Baseline. |
| `round_robin` | Baseline. Ignores every signal on purpose. |

The last three are cache-blind. Read [Why is my bill higher?](caching.md) before
choosing one.

`cost_tiered` deserves its own warning. Its cost term is normalised across the
eligible pool, so the *size* of a tier gap is erased: accounts at 1.0 and 1.05 score
exactly as far apart as accounts at 1.0 and 5.0. With cost weighted at 0.80 against
affinity's 0.20 it will re-home an established conversation over a 5% tier
difference, and a cache break costs far more than 5%. `sticky_headroom` already
prefers the cheaper account when placing a *new* session, which is the part worth
having — so set `cost_tier` and leave the strategy alone unless your tiers differ by
more than the cache penalty.

`tokenbiryani strategies` lists what your install actually has, including plugins.

## Leases

Scoring alone is not enough. Without an atomic reservation, N concurrent requests all
read the same "plenty of headroom" and stampede one account into a 429.

So the estimated cost is **reserved before dispatch** and released on response, then
reconciled against actual usage. Estimates are deliberately pessimistic
(`routing.estimate_safety_margin`, default 1.15): reserving slightly too much is the
safe direction.

### Sizing the output half

Input is approximated from the body. Output has no such handle before the request
runs — only the caller's `max_tokens`, which is a ceiling and not a forecast. An
agent client sends 32,000 and returns a few hundred tokens.

Reserving the ceiling costs most of the pool's concurrency:

| Output window | `max_tokens` | Concurrent requests admitted |
|---|---|---|
| 16,000 | 8,192 | 1 |
| 64,000 | 32,000 | 2 |

The lease is released afterwards, so no quota is *spent* on the difference — the cost
is paid during the request. An account leased to its ceiling reads as full, an account
that reads as full is filtered out of routing, and a conversation whose owner is
filtered out gets re-homed onto a credential that has never seen its prefix. An
estimation problem becomes a cache break, which is the expensive kind.

So `routing.output_estimate: adaptive` (the default) keeps a rolling sample of what
each model really returns and leases a high quantile of it. Three things bound it:

- **Never above the caller's `max_tokens`.** It can only ever reserve less than the
  old behaviour, never more.
- **The ceiling until there is evidence** — `output_estimate_min_samples`, default 20.
- **The ceiling again if it is being beaten**, past
  `output_estimate_max_undershoot`. A p95 predictor is outrun about 5% of the time by
  construction; this catches a distribution that has changed shape.

`GET /admin/estimation` shows what it believes per model, including whether it is
predicting at all. `routing.output_estimate: max_tokens` restores the old behaviour.

Admission is unaffected. `max_tokens` larger than an account's entire output window
still makes that account unable to serve the request — predicting sizes the lease, it
does not overrule a bound the caller stated.

## Session affinity

The affinity key is the `X-TokenBiryani-Session` header when present, otherwise a
fingerprint of the request's stable head — system prompt, tool names, and the first
two messages. That stays constant as a conversation grows, so no client changes are
needed.
