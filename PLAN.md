# Biryani — a Claude account pooling gateway

> Many accounts, one pot. A drop-in Anthropic-compatible endpoint that spreads your
> traffic across every Claude credential you own and keeps working when one runs dry.

---

## 1. What it is

A single local/self-hosted HTTP service that speaks the **Anthropic Messages API**
verbatim. Your tools point at it instead of `api.anthropic.com`:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8787
export ANTHROPIC_AUTH_TOKEN=bir_your_virtual_key
claude          # Claude Code now runs through the pool
```

Behind that endpoint sits a pool of credentials. Every request is admitted, scored,
routed, retried, and accounted for. Nothing about the request body changes — only the
auth header and the upstream it lands on.

**Design axiom: the gateway is a router, not a rewriter.** It never edits prompts,
tools, or parameters. That single rule is what keeps it correct across API changes and
makes it safe to sit in a hot path.

---

## 2. Positioning (why this is not "worse LiteLLM")

Prior art: LiteLLM, Portkey, Helicone, OpenRouter, various nginx round-robin hacks.
They are all *breadth* plays — many providers, lowest-common-denominator routing
(round-robin / least-busy / cheapest).

Biryani is a *depth* play on one provider. Four things fall out of that focus and are
hard to retrofit into a generic proxy:

| | Biryani | Generic proxy |
|---|---|---|
| **Routing signal** | Live mirror of `anthropic-ratelimit-*` response headers — real remaining request/input-token/output-token budget and reset times, per account | Round-robin or reactive 429 backoff |
| **Prompt-cache awareness** | Session affinity keeps a conversation on the account that holds its cache; cache-break is a tracked SLI | Round-robin shreds the cache silently |
| **Claude Code as first-class client** | Long streaming turns, huge cached system prompts, tool loops — the exact traffic shape it is tuned for | Treated as generic chat completion |
| **Mixed pool** | API keys + Bedrock + Vertex in one pool, with capability and cost tiering | Usually one credential type per route |

If you cannot defend that table, the project has no reason to exist. Put it in the
README above the fold.

---

## 3. The non-obvious constraint: prompt caching

This is the design decision most likely to be missed, and it dominates the economics.

Anthropic's prompt cache is scoped **per credential/organization**. A Claude Code
conversation sends a large, stable prefix (system prompt + tool definitions + history)
on every turn. With a warm cache those tokens bill at ~10% of input rate. Naïvely
round-robining turns across N accounts turns nearly every turn into a cache miss.

A gateway that "balances load" without cache awareness can make a workload **several
times more expensive and noticeably slower** than not using the gateway at all. So:

- **Default routing strategy is sticky, not balanced.** Affinity first; rebalance only
  when the sticky account cannot serve the request.
- **Session key** = explicit `X-TokenBiryani-Session` header if present, else a fingerprint
  hash of the stable request prefix (system block + tools block + first *k* messages).
  Requires no client changes to work with Claude Code.
- **Cache-break is a metric, not a shrug.** Every forced affinity break emits an event;
  `cache_read_input_tokens` / `cache_creation_input_tokens` from the response `usage`
  block feed a per-account cache hit rate on the dashboard.

Breaking affinity is a real cost the router must weigh against the benefit of moving,
not a free action.

---

## 4. Architecture

```
                 ┌──────────────────────────────────────────────────┐
   Claude Code   │               TOKENBIRYANI                       │
   SDK / curl ──▶│  auth ─▶ admission ─▶ router ─▶ proxy ─▶ upstream│──▶ acct A (API key)
                 │            │            │         │              │──▶ acct B (API key)
                 │            ▼            ▼         ▼              │──▶ acct C (Bedrock)
                 │          queue      registry   retry/            │──▶ acct D (Vertex)
                 │                     + limits   failover          │
                 │                        │         │               │
                 │                        ▼         ▼               │
                 │                   state store  events ─▶ metrics │
                 └──────────────────────────────────────────────────┘
```

### Request lifecycle

1. **Auth** — client presents a *virtual key* (`bir_…`), never a real upstream
   credential. Virtual keys carry: allowed models, pool scope, RPM cap, spend cap.
2. **Admission control** — estimate request cost (input tokens + `max_tokens`
   reservation). If no account in scope could *ever* serve it, fail fast with a clear
   error rather than queueing forever.
3. **Route** — filter to eligible accounts, score, pick. (§5)
4. **Lease** — atomically reserve the estimated token/request budget on the chosen
   account so concurrent requests cannot collectively overshoot its limit.
5. **Proxy** — stream bytes through untouched, swapping only auth headers.
6. **Reconcile** — parse `anthropic-ratelimit-*` headers and the `usage` block; update
   the account's limit mirror, release the lease, record cost, latency, TTFT, cache hits.
7. **On failure** — classify, apply the account state transition, decide retry. (§6)

### Modules

```
tokenbiryani/
  api/            # FastAPI routes: /v1/messages, /v1/messages/count_tokens,
                  #   /v1/models, /admin/*, /metrics, /healthz
  core/
    registry.py   # account registry, config load + hot reload
    account.py    # Account model, capabilities, health state machine
    limits.py     # rate-limit mirror, token estimation, atomic leases
    router.py     # Strategy protocol + implementations
    breaker.py    # per-account circuit breaker
    queue.py      # admission control + priority wait queue
    session.py    # affinity keying / prefix fingerprinting
  providers/
    base.py       # Upstream protocol: send / stream / parse_limits / classify_error
    anthropic_api.py
    bedrock.py
    vertex.py
    oauth.py      # opt-in, off by default — see §11
  proxy/
    passthrough.py  # streaming relay, first-token buffering, transparent retry
    errors.py       # error taxonomy → action mapping
  store/
    memory.py  sqlite.py  redis.py    # one StateStore interface
  observability/
    metrics.py  logs.py  events.py
  dashboard/      # static SPA fed by SSE
  cli.py
```

Two interfaces carry the whole extensibility story and must be defined in M1, before
they have implementations to be shaped by:

- `Upstream` — anything that can serve a Messages request and report its limits.
- `StateStore` — anything that can hold account state, leases, affinity, and the queue.
  **All shared state goes through it from day one.** Scattering `dict` access across
  modules is the one refactor that would later cost weeks.

---

## 5. Routing

### The availability signal

Every Anthropic API response carries the account's live budget:

```
anthropic-ratelimit-requests-limit / -remaining / -reset
anthropic-ratelimit-input-tokens-limit / -remaining / -reset
anthropic-ratelimit-output-tokens-limit / -remaining / -reset
retry-after                      (on 429)
```

The gateway keeps a per-account mirror of these, updated on **every** response. That
gives real-time availability with zero extra probing — no health-check traffic, no
guessing. This is the single most valuable thing about being Claude-specific.

### Selection

**Filter** — an account is eligible if it is `HEALTHY`, in the virtual key's pool scope,
supports the requested model and required capabilities (vision, tool use, extended
context, extended thinking), is within its spend cap, and has projected headroom
≥ estimated cost.

**Score** —

```
score =  w_affinity  · is_sticky_owner
       + w_headroom  · min(token_headroom_frac, request_headroom_frac)
       + w_priority  · account_priority
       - w_load      · inflight / max_concurrency
       - w_errors    · recent_error_rate
       - w_cost      · normalized_cost_per_token
```

Weights are config, not code. Ties break on a deterministic hash of the session key so
selection is stable and reproducible under replay.

### Strategies (pluggable, `entry_points` for third-party ones)

- `sticky_headroom` — **default**; affinity, then most headroom.
- `headroom` — pure most-available, no affinity. For stateless batch traffic.
- `cost_tiered` — drain cheap/free-tier accounts first, spill upward.
- `priority` — strict ordered failover (primary → backup).
- `round_robin`, `least_loaded` — baselines, mostly for benchmarking against the above.

Ship the baselines specifically so the README can show measured numbers for why the
default wins.

---

## 6. Failure handling

### Error taxonomy → action

| Upstream | Account action | Request action |
|---|---|---|
| `429` rate_limit | cooldown until `retry-after` / `reset` | retry on another account |
| `529` overloaded | short cooldown + backoff | retry, same or other |
| `500` / `502` / `503` | error-rate++, breaker may trip | retry with jitter |
| `401` / `403` invalid auth | **disable**, alert operator | retry elsewhere |
| `403` model not permitted | mark model unsupported for this account | retry elsewhere |
| `400` invalid_request | none — not the account's fault | **return to client, never retry** |
| `413` / context length | none | return to client |
| timeout / connection | error-rate++ | retry, budget-bounded |

Retrying a `400` across every account in the pool is the classic failure-amplification
bug in gateways like this. The taxonomy exists to prevent exactly that.

**Retry budget:** max attempts, max distinct accounts, and a wall-clock deadline —
whichever binds first. Exponential backoff with full jitter.

**Circuit breaker** per account: closed → open on consecutive failures → half-open
single probe after cooldown → closed on success.

### The streaming retry boundary

Once the first SSE byte reaches the client, transparent failover is impossible — the
client has already begun rendering a response.

So: hold the upstream stream and buffer until either the first `content_block_delta`
arrives or a short grace window elapses. A failure **before** that point is retried
silently on another account; the client never knows. A failure **after** it surfaces as
an SSE `error` event and the request ends.

That boundary should be documented prominently, because it is the one case where the
gateway cannot fully hide a failure, and users will hit it.

---

## 7. Queueing and backpressure

When no account passes admission:

- **Bounded priority queue.** Interactive traffic outranks batch. Each entry carries a
  `max_wait` (client-supplied or per-key default).
- **Wake on signal, not on poll.** A waiter is woken when an account's rate-limit
  `reset` timestamp elapses or an inflight request completes and releases its lease.
- **Honest backpressure.** Queue full or deadline exceeded → `429` with an accurate
  `retry-after` computed from the earliest upcoming reset. Never silently accumulate
  unbounded latency; a caller that knows it will wait 4 minutes can make a better
  decision than one left hanging.
- **Batch spill lane** (later): non-urgent requests can be diverted to the Message
  Batches API at ~50% cost instead of waiting.

---

## 8. Observability

The dashboard is a stated requirement, so the event stream is designed first and the UI
renders it — not the reverse.

**Per account:** state, limits vs. remaining (requests / input tokens / output tokens),
reset countdown, inflight, p50/p95 latency, time-to-first-token, error rate by class,
tokens and spend by model, cache hit rate.

**Global:** RPS, queue depth and wait-time histogram, failover count, **cache-break
count**, spend burn-down against caps, per-virtual-key attribution.

**Surfaces:** Prometheus `/metrics`; structured JSON logs keyed by `request_id` with the
full attempt chain; a built-in single-page dashboard fed by SSE; optional OpenTelemetry
traces.

**Privacy default: prompt bodies are never logged.** Opt-in sampling only, with
redaction. A tool that sits in the prompt path and logs by default is one incident away
from being untrustworthy.

---

## 9. Security

- Clients get **virtual keys**; real upstream credentials never leave the gateway.
- Credentials encrypted at rest (libsodium sealed box / `age`), key from env or OS
  keyring. Never logged, redacted in every error path and in dashboard output.
- Per-key scoping: models, pool, RPM, spend cap.
- Bind to loopback by default. Refuse to start on `0.0.0.0` without an explicit
  `--allow-remote` plus auth configured — the common self-host footgun.
- `SECURITY.md` with a private reporting path from day one; this project holds secrets.

---

## 10. Persistence and scale

- **Single process (default):** in-memory state + SQLite for config, audit, and spend.
  Zero external dependencies — `pipx install` and go.
- **Multi-instance:** Redis-backed `StateStore` for shared limit mirrors, leases,
  affinity map, and queue. Because everything already routes through the interface, this
  is an implementation, not a rewrite.
- **Leases are the correctness mechanism.** Without atomic reservation, N concurrent
  requests all see the same "plenty of headroom" and stampede one account into a 429.
  Reserve on dispatch, reconcile against actual `usage` on response.

---

## 11. Credential types, and the honest caveat

Two adapter families:

**`api_key` — the supported path.** Multiple Anthropic API keys, workspaces, and orgs,
plus Bedrock and Vertex members in the same pool. Real rate-limit headers, clean quota
semantics, squarely within normal API usage. Everything above is designed for this.

**`oauth` — Claude Pro/Max subscription sessions.** Technically pluggable, and the
architecture accommodates it, but two things are worth stating plainly:

1. Pooling subscription accounts to exceed per-account limits runs against Anthropic's
   consumer terms. For an open-source project that will be publicly associated with your
   name, that is a reputational and takedown risk, not just a legal footnote.
2. It routes badly. Subscription sessions expose no `anthropic-ratelimit-*` headers, so
   the intelligent routing that is the project's whole differentiator degrades to
   reactive 429 backoff.

**Recommendation:** ship API keys (+ Bedrock/Vertex) as the supported, documented,
tested path. Keep the `Upstream` interface open so an OAuth adapter *can* exist, but
leave it out of the core repo, or ship it disabled behind an explicit opt-in flag with a
notice. This costs nothing architecturally and keeps the project publishable and
sponsorable. Your call — the design does not change either way.

---

## 12. Milestones

Each milestone is independently demoable. M0 exists to kill the project fast if the
premise does not hold.

| | Milestone | Ships | Proves |
|---|---|---|---|
| **M0** | Spike | Passthrough proxy, one account, SSE streaming intact | Claude Code works unmodified through the gateway |
| **M1** | Pool | N accounts, round-robin, health state machine, error taxonomy, retry/failover, **mock upstream harness** | Failover works and is testable |
| **M2** | Smart routing | Limit mirror, token estimation, leases, headroom scoring, circuit breaker | Routing beats round-robin on measured 429 rate |
| **M3** | Continuity | Session affinity, prefix fingerprinting, cache metrics | Cache hit rate stays high across a multi-turn session |
| **M4** | Queue | Admission control, priority queue, deadlines, backpressure | Graceful behavior when the whole pool is saturated |
| **M5** | Observability | `/metrics`, structured logs, SSE dashboard | Operators can see and trust it |
| **M6** | Multi-tenant | Virtual keys, spend caps, Redis store, multi-instance | Usable by a team, not just one laptop |
| **M7** | Breadth | Bedrock/Vertex adapters, batch spill lane, strategy plugins | Ecosystem surface |

**M1 must include the mock upstream.** A fake Anthropic server that emits configurable
rate-limit headers, 429s with `retry-after`, 529s, slow streams, and mid-stream
disconnects. Without it, every routing behavior is untestable except against the real
API with real money, and the project stalls at M2. This is the highest-leverage piece of
infrastructure in the plan and it is easy to defer by accident.

---

## 13. Open-source mechanics

**License:** Apache-2.0. The explicit patent grant matters for infrastructure that
companies may adopt internally; MIT is the alternative if you want maximum simplicity
and no NOTICE handling.

**Name:** publish the package as `tokenbiryani` (or `tokenbiryani`), described as "a
pooling gateway for Claude." Avoid `claude-*` as the *distribution* name — "Claude" is
an Anthropic trademark and a `claude-` prefixed package implies affiliation, which is
the kind of thing that gets a package renamed later under pressure. Keep the repo folder
name; change the published identity.

**Repo scaffolding:** README with a 60-second quickstart above the fold; the §2
positioning table right under it; `CONTRIBUTING.md`; Contributor Covenant;
`SECURITY.md`; `CHANGELOG.md` (Keep a Changelog); issue/PR templates; ADRs in
`docs/adr/` for the decisions in §3, §6, and §11, because those will be re-litigated by
every new contributor and you want one link to point at.

**CI:** GitHub Actions — ruff, mypy/pyright, pytest with coverage gate, Docker build,
release on tag via PyPI trusted publishing + GHCR image. `docker compose up` as the
alternate one-command path.

**Docs:** mkdocs-material. Non-negotiable pages: quickstart, configuration reference,
routing strategies, **the streaming-retry boundary**, and a "why is my bill higher"
cache-affinity explainer.

**Contributor surface:** routing strategies and provider adapters are the natural
`good first issue` territory — both are behind clean interfaces with the mock harness
available to test against. Say so explicitly in CONTRIBUTING.

---

## 14. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Cache fragmentation makes the gateway *cost more* than no gateway | **High** | Affinity by default; cache hit rate as a headline metric from M3; publish a before/after benchmark |
| Subscription pooling is a ToS problem | **High** | Ship the API-key path; §11 |
| Token estimation error → overshoot and 429 storms | Medium | Conservative leases, reconcile on actual usage, adaptive safety margin |
| Streaming retry cannot be transparent post-first-token | Medium | Buffer to first delta; document the boundary |
| Upstream API drift | Low | Passthrough design — never parse or rebuild request bodies |
| Trademark on the name | Low | Rename the distribution, not the repo |
| "It's just LiteLLM" perception | Medium | Lead the README with §2; ship the benchmark |

---

## 15. First decisions to make

1. **Stack** — Python/FastAPI (fast iteration on routing heuristics, best fit for a
   plan this heuristic-heavy), Node/TS (one language if the dashboard grows), or Go
   (throughput + single binary, slower iteration).
2. **Credential scope** — API keys only, or keep the OAuth door open per §11.
3. **License** — Apache-2.0 vs MIT.
4. **Published name.**

Answer those four and M0 is a day's work.
