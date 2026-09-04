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

> The one exception is the Bedrock and Vertex adapters. Those platforms address the
> model in the URL and stamp their own `anthropic_version`, so exactly two fields are
> translated, in one file (`providers/translate.py`), and nowhere else. Everything else
> the caller sent — including parameters this gateway has never heard of — travels
> through untouched.

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
`tokenbiryani strategies` lists what this install has, including any installed plugins:
a strategy can ship in its own package under the `tokenbiryani.strategies` entry point,
supplying either weights or its own scoring.

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

Open **`http://localhost:8787/console`** for the pool, the capacity horizon, a live
request feed, the routing inspector, per-account detail, and key management. It is one
server-rendered HTML file inside the package — no build step, no Node toolchain added to
a `pipx install`. The shell carries no data and needs no key; it asks for an admin key on
first load and keeps it in that browser only.

Or stay in the terminal:

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
| `GET /console` | the operator console |
| `POST /v1/messages` | Messages API, streaming and not |
| `POST /v1/messages/count_tokens`, `GET /v1/models` | passthrough |
| `GET /healthz` | 200 while any account is ready |
| `GET /metrics` | Prometheus |
| `GET /admin/status` | pool snapshot |
| `POST /admin/keys` · `DELETE /admin/keys/{name}` | mint and revoke keys at runtime |
| `GET /admin/accounts/{id}` | one account: limits, error breakdown by class, its own recent requests |
| `POST /admin/reload` | re-read the config file |
| `GET /admin/requests/{id}` | **why that request went where it did** — attempt chain, per-candidate scores, verdicts |
| `GET /admin/horizon` | projected capacity for the next hour |
| `GET /admin/events` | live SSE feed |

The request inspector is the point. `filtered — cooling, 27s remaining` is a complete
answer; "load balanced" is not.

**`/admin/*` requires a key with `admin: true`.** It exposes account ids, spend and key
management, so a tenant key must not reach it. A gateway with no keys configured at all
is loopback development mode and stays fully open.

### Managing keys at runtime

```bash
curl -sX POST localhost:8787/admin/keys -H "x-api-key: $ADMIN_KEY" \
  -d '{"name":"tenant-1","pool":["acct-02"],"rpm":60,"spend_cap_usd":5}'
# -> {"key": "bir_...", "record": {...}}   the plaintext appears exactly once

curl -sX DELETE localhost:8787/admin/keys/tenant-1 -H "x-api-key: $ADMIN_KEY"
```

Minted keys are stored **hashed**, so a leaked state store is not a leaked key, and they
live in the shared store — one instance honours a key another minted. Keys declared in
the config file belong to the file: the API will not revoke them.

### The spill lane

With `batch.enabled: true`, a `batch`-priority request that finds the pool saturated goes
to the **Message Batches API** instead of waiting. The gateway holds the connection while
it polls, bounded by that request's own wait budget. A batch that outlives the budget is
**cancelled upstream** and its id returned in `x-tokenbiryani-batch-id`, so nothing is
silently abandoned. If submission fails the request falls back to the normal queue — the
spill lane is an optimisation, never a dependency. Streaming requests never spill.

### Account types

| `type` | Notes |
|---|---|
| `anthropic_api` | Anthropic API keys. The default. |
| `bedrock` | AWS Bedrock. SigV4-signed; its binary event-stream is decoded back to SSE so the rest of the gateway sees ordinary streaming. Needs `pip install "tokenbiryani[bedrock]"`. |
| `vertex` | Google Vertex AI. Bearer token from application-default credentials; returns real SSE already. Needs `pip install "tokenbiryani[vertex]"`. |

All three sit in one pool, so a request can fail over from an API key to Bedrock. Use
`options.model_map` to translate your callers' model names into each platform's ids.

```yaml
accounts:
  - id: acct-01
    type: anthropic_api
    api_key: ${ANTHROPIC_API_KEY}
  - id: acct-bedrock
    type: bedrock
    cost_tier: 1.2
    options:
      region: us-east-1
      model_map:
        claude-test-1: anthropic.claude-3-5-sonnet-20241022-v2:0
  - id: acct-vertex
    type: vertex
    options:
      project: my-project
      region: us-central1
```

### Request headers

| Header | |
|---|---|
| `X-TokenBiryani-Session` | pin a conversation to one affinity key instead of the fingerprint |
| `X-TokenBiryani-Priority` | `interactive` (default) or `batch`. Batch traffic yields the queue to interactive traffic when the pool is saturated |
| `X-TokenBiryani-Max-Wait` | seconds this request will wait for capacity. A client can shorten its own budget but never extend it past the operator's ceiling |

Per-key defaults for the last two live under `keys:` as `priority` and `max_wait_seconds`.

### Spend caps

Caps are **windowed, not lifetime** (`spend.window_hours`, default 24). A lifetime cap on
a persistent store would eventually wedge the gateway shut and stay that way.

They only survive a restart if the store does. `store.backend: memory` (the default)
loses affinity and the spend ledger when the process dies — meaning every cap silently
resets. Use `sqlite` for a real deployment:

```yaml
store:
  backend: sqlite
  path: tokenbiryani.db
```

### Running more than one instance

`store.backend: redis` shares affinity, the spend ledger and per-key rate counters
between processes. Without it two instances each keep their own affinity map — so a
conversation ping-pongs between them and loses its cache — and each enforces its own
half of every spend cap.

```yaml
store:
  backend: redis
  url: redis://127.0.0.1:6379/0
  namespace: tokenbiryani
```

Needs the optional dependency: `pip install "tokenbiryani[redis]"`.

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
structured logs, config hot reload, the admin API, and the CLI. Request priority with a
per-request wait budget, a batch spill lane, and SQLite-backed persistence for affinity
and windowed spend, a Redis store for multi-instance deployments, and runtime key
management behind an admin boundary, plus Bedrock and Vertex adapters and pluggable
routing strategies, and the operator console. 190 tests, plus an end-to-end smoke test
over real sockets (`scripts/smoke.sh`).

All eight milestones in `PLAN.md` are built. `docs/UI-DESIGN.md` is the console's design
brief, and the console follows it.

**Credential types.** Anthropic API keys are the supported path. Pooling Pro/Max
*subscription* accounts runs against Anthropic's consumer terms, and those sessions
expose no rate-limit headers — which would degrade the routing this project exists for
into reactive backoff. The `Upstream` interface is open if you want to go there; nothing
in this repo does.

## License

Apache-2.0.
