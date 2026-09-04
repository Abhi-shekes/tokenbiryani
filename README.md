# tokenbiryani

**A pooling gateway for Claude accounts.** One Anthropic-compatible endpoint in front of
every credential you own. It routes on live rate-limit headers, keeps a conversation on
the account holding its prompt cache, fails over without dropping a stream, and queues
honestly when the whole pool is dry.

```bash
pip install tokenbiryani          # or: pipx install tokenbiryani
tokenbiryani init                 # writes tokenbiryani.yaml + a virtual key
tokenbiryani serve

export ANTHROPIC_BASE_URL=http://localhost:8787
export ANTHROPIC_AUTH_TOKEN=bir_...   # printed by `init`
claude                            # Claude Code now runs through the pool
```

No client changes. The gateway speaks the Messages API verbatim — it swaps the auth
header and picks an upstream, and touches nothing else in the request.

---

## Why not just use a generic proxy

LiteLLM, Portkey and friends are breadth plays: many providers, lowest-common-denominator
routing. This is a depth play on one provider, and four things fall out of that.

| | tokenbiryani | Generic proxy |
|---|---|---|
| **Routing signal** | Live mirror of `anthropic-ratelimit-*` headers — real remaining request and token budget, per account, per reset window | Round-robin, or reactive backoff after a 429 lands |
| **Prompt cache** | Session affinity keeps a conversation on the account holding its cache; cache-break is a tracked metric | Round-robin shreds the cache silently |
| **Claude Code** | First-class client — long streaming turns, huge cached prefixes, tool loops | Treated as generic chat completion |
| **Failover** | Transparent up to the first streamed token, with an explicit documented boundary | Usually all-or-nothing |

### The prompt cache is the constraint

Anthropic's cache is scoped per credential. A Claude Code turn resends a large stable
prefix each time; warm, it bills at roughly a tenth of the input rate. **A gateway that
balances load without cache awareness can cost several times more than no gateway at
all.** So the default strategy is sticky, not balanced: affinity first, rebalance only
when the owner genuinely cannot serve. Every forced break is counted.

---

## How a request flows

```
auth → admit → route → lease → proxy → reconcile → recover
```

1. **Auth** — a virtual key (`bir_…`); real credentials never leave the process.
2. **Admit** — estimate cost; if no account could *ever* serve it, fail fast rather than
   queue forever.
3. **Route** — filter to eligible accounts, score, pick.
4. **Lease** — atomically reserve the estimate so concurrent requests can't collectively
   overshoot one account into a 429.
5. **Proxy** — stream bytes through untouched.
6. **Reconcile** — parse limit headers and usage; update the mirror, release the lease.
7. **Recover** — classify the failure, transition the account, decide whether to retry.

### Routing strategies

Set `routing.strategy`:

| Strategy | Behaviour |
|---|---|
| `sticky_headroom` | **Default.** Affinity, then most headroom. |
| `headroom` | Pure most-available. Correct for stateless batch traffic. |
| `cost_tiered` | Drain cheap accounts first, spill upward. |
| `priority` | Strict ordered failover: primary, then backup. |
| `least_loaded` | Baseline. |
| `round_robin` | Baseline. Ignores every signal, on purpose — it's there to benchmark against. |

Weights are config, not code. See `routing.weights` in `tokenbiryani.example.yaml`.

### Failure handling

| Upstream | Account | Request |
|---|---|---|
| `429` | cooldown until `retry-after` | retry elsewhere |
| `529` | short cooldown | retry |
| `500`/`502`/`503` | error tick, breaker may trip | retry with jitter |
| `401`/`403` auth | **disabled**, operator alerted | retry elsewhere |
| `403` model denied | model marked unsupported here | retry elsewhere |
| `400` / `413` | none — not the account's fault | **returned as-is, never retried** |

Retrying a `400` across the whole pool is the classic amplification bug in gateways like
this. The taxonomy exists to prevent exactly that.

### The one failure that can't be hidden

Once the first SSE byte reaches the client, transparent failover is impossible — the
client is already rendering. So the gateway buffers the upstream stream until the first
`content_block_delta` (or `retry.first_token_grace_seconds`). A failure **before** that
point is retried silently on another account. A failure **after** it arrives as an SSE
`error` frame and the request ends.

---

## Operating it

```bash
tokenbiryani status          # the pool, in the terminal you're already in
tokenbiryani status --json   # same data, for scripts
```

```
  POOL  702k tok ready · next reset 00:12 · queue 0 · cache 94%

  ACCOUNT   STATE            REQ    INPUT    OUTPUT   RESET   CACHE
  acct-01   ● ready          98%      82%       79%   00:41    97%
  acct-02   ● cooling         4%       0%        6%   00:27     —

  1h  412 requests · 3 failovers · 1 cache break · 0 errors · $18.40
```

| Endpoint | |
|---|---|
| `POST /v1/messages` | Messages API, streaming and not |
| `POST /v1/messages/count_tokens`, `GET /v1/models` | passthrough |
| `GET /healthz` | 200 while any account is ready |
| `GET /metrics` | Prometheus |
| `GET /admin/status` | pool snapshot |
| `GET /admin/accounts/{id}` | one account: limits, error breakdown by class, its own recent requests |
| `POST /admin/reload` | re-read the config file |
| `GET /admin/requests/{id}` | **why that request went where it did** — attempt chain, per-candidate scores, verdicts |
| `GET /admin/horizon` | projected capacity for the next hour |
| `GET /admin/events` | live SSE feed |

The request inspector is the point. `filtered — cooling, 27s remaining` is a complete
answer; "load balanced" is not.

### The spill lane

With `batch.enabled: true`, a `batch`-priority request that finds the pool saturated goes
to the **Message Batches API** instead of waiting. The gateway holds the connection while
it polls, bounded by that request's own wait budget. A batch that outlives the budget is
**cancelled upstream** and its id returned in `x-tokenbiryani-batch-id`, so nothing is
silently abandoned. If submission fails the request falls back to the normal queue — the
spill lane is an optimisation, never a dependency. Streaming requests never spill.

### Request headers

| Header | |
|---|---|
| `X-TokenBiryani-Session` | pin a conversation to one affinity key instead of the fingerprint |
| `X-TokenBiryani-Priority` | `interactive` (default) or `batch`. Batch traffic yields the queue to interactive traffic when the pool is saturated |
| `X-TokenBiryani-Max-Wait` | seconds this request will wait for capacity. A client can shorten its own budget but never extend it past the operator's ceiling |

Per-key defaults for the last two live under `keys:` as `priority` and `max_wait_seconds`.

### Costs

The gateway ships **no price list**. Costs are reported and spend caps enforced only for
models you name under `pricing:` in the config. Baking prices into code would mean
silently billing against stale numbers.

### Privacy and security

- Prompt bodies are **never logged**. Only accounting: ids, accounts, tokens, latency.
- Credentials are never logged and are masked in every admin response.
- Binds to loopback. Refuses to start on a public interface without both
  `server.allow_remote: true` and at least one configured key.

---

## Status

Working today: passthrough and streaming, multi-account pooling, the error taxonomy,
retry and failover, the rate-limit mirror, token estimation and leases, headroom scoring,
circuit breakers, session affinity and cache accounting, admission control and a bounded
priority queue, virtual keys with model/pool/rpm/spend scoping, Prometheus metrics,
structured logs, config hot reload, the admin API, and the CLI. 95 tests, plus an
end-to-end smoke test over real sockets (`scripts/smoke.sh`).

Not built yet: the web console, the Redis state store for multi-instance, Bedrock and
Vertex adapters, and the Message Batches spill lane. See `PLAN.md` for the roadmap and
`docs/UI-DESIGN.md` for the console design.

**Credential types.** Anthropic API keys are the supported path. Pooling Pro/Max
*subscription* accounts runs against Anthropic's consumer terms, and those sessions
expose no rate-limit headers — which would degrade the routing this project exists for
into reactive backoff. The `Upstream` interface is open if you want to go there; nothing
in this repo does.

## License

Apache-2.0.
