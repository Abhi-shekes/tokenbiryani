# tokenbiryani

**A pooling gateway for Claude accounts.** One Anthropic-compatible endpoint in front of
every credential you own. It routes on live rate-limit headers, keeps a conversation on
the account holding its prompt cache, fails over without dropping a stream, and queues
honestly when the whole pool is dry.

```bash
pip install tokenbiryani          # or: pipx install tokenbiryani
tokenbiryani init                 # writes tokenbiryani.yaml + a virtual key
tokenbiryani serve                # starts with an empty pool
tokenbiryani console              # opens the browser, already signed in
```

![The console's Overview screen: readiness, next reset, queue depth, cache hit rate and
spend across the top, a capacity horizon below it, and every account in the pool with
its headroom meters, p95, cache rate and spend](docs/images/console-overview.png)

Nothing needs exporting first. The console's wizard takes your first credential,
**verifies it** — the same rate-limit-header check `tokenbiryani doctor` performs — and
hands you the two lines that use it, with a working key already in them:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8787
export ANTHROPIC_AUTH_TOKEN=bir_...
claude                            # Claude Code now runs through the pool
```

Prefer the terminal:

```bash
tokenbiryani accounts add work --api-key sk-ant-...   # probed before it is stored
tokenbiryani accounts test                            # all of them, headers included
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

### The benchmark

The same workload — 24 concurrent conversations, 8 turns each, across 4 accounts —
under each strategy, against a mock upstream that models Anthropic's per-credential
prompt cache. Prices are illustrative ratios, not a price list.

| Strategy | Cache hit | Cache breaks | Cost | vs sticky | Billed input |
|---|---|---|---|---|---|
| sticky_headroom | 78.6% | 0 | $0.4923 | — | 433,152 |
| round_robin | 47.8% | 144 | $0.9535 | 1.94x | 433,152 |
| least_loaded | 47.8% | 144 | $0.9535 | 1.94x | 433,152 |
| headroom | 47.8% | 144 | $0.9535 | 1.94x | 433,152 |

**Cache-blind routing costs 1.94x here.** And note that round-robin, least-loaded and
most-headroom all pay exactly the same penalty: any strategy that ignores affinity
visits every account once per conversation, so they all take the same number of cache
misses. The penalty is inherent to cache-blindness, not a quirk of round-robin.

Reproduce it with `python benchmarks/cache_affinity.py`. `tests/test_benchmark.py` fails if sticky ever stops winning.

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

## Docker

```bash
docker compose up -d      # gateway + Redis + a mock Anthropic
docker compose down       # stop it
```

Nothing to set first. The stack boots against the mock upstream bundled with the
package, so it comes up healthy, costs nothing and reaches nothing — then you open
`http://localhost:8787/console` and add real accounts there.

`./src` is bind-mounted and watched, so editing a file on the host restarts the
gateway in about a second; the console's HTML and CSS need only a browser refresh;
and `./tests` is mounted too, so `docker compose exec gateway pytest -q` runs the
suite against the running stack.

The image runs as a non-root user, carries a healthcheck wired to `/healthz`, and
publishes to `127.0.0.1` rather than your LAN. `docker compose` builds the Dockerfile's
`dev` target; the default `runtime` target is what ships — wheel only, no source, no
test dependencies.

> The keys in `docker-compose.yml` are development values in a public repository.
> Copy `.env.example` to `.env` and replace them before this touches anything real —
> in particular `TOKENBIRYANI_SECRET_KEY`, which encrypts stored account credentials
> and must outlive the container. [docs/deployment.md](docs/deployment.md) explains both.

## Operating it

Open **`http://localhost:8787/console`**. Everything the gateway can do, it can do from
there:

- **Accounts** — add, name, test, rename, rotate, enable, disable and delete
  credentials without editing a file. Anthropic API keys, Bedrock, Vertex, and Claude
  subscriptions via a browser login. Credentials are encrypted at rest; accounts
  declared in `tokenbiryani.yaml` render locked, because the file is yours.
- **Usage** — token, cost and cache-hit-rate charts over 1h / 24h / 7d / 30d, grouped
  by account, model or virtual key, with a totals table. This history is persisted, so
  it survives a restart. See [docs/usage.md](docs/usage.md).
- **Overview** — capacity horizon, live request feed, per-account meters.
- **Requests** — the routing inspector: why each request went where it did.
- **Connect a client** — the exact export lines for this gateway's address.
- **Keys** — mint and revoke virtual keys.

![The Accounts screen: the pool, each account's state and where it was declared, its
cost tier, priority and spend, with Test and Edit on every
row](docs/images/console-accounts.png)

Click an account and it opens: its limits, what its meters can and cannot tell you,
where its credential came from, an error breakdown by class, and its own recent
requests.

![An account detail panel: a subscription account explaining that it reports
rolling-window utilisation rather than per-window budgets, where its token file lives
and when it expires, then state, requests, failures, p95, cache hit and spend, over its
recent requests](docs/images/console-account-detail.png)

Settings shows what the gateway is *currently running* — strategy and price table, each
marked with where its value came from, and a Reload that re-reads the file.

![The Settings screen: routing strategy and price table, both marked "from the file",
with the bundled table's date beside it and live counts of accounts, queue depth and
priced models](docs/images/console-settings.png)

It is one server-rendered page plus a stylesheet, inside the package — no build step, no
Node toolchain added to a `pipx install`. The shell carries no data and needs no key; it
asks for an admin key on first load and keeps it in that browser only.

### Does it actually work against the real API?

The one thing no mock can tell you is whether Anthropic spells its rate-limit headers
the way the router expects. If it doesn't, the mirror stays empty, every account reads
as full, and routing quietly degrades to round-robin — shredding the prompt cache while
looking healthy.

```bash
tokenbiryani doctor --api-key sk-ant-...
```

One real request, `max_tokens=1`. It prints the headers the upstream actually returned
next to the nine the limit mirror looks for, and what the mirror parsed out of them.
Non-zero exit if anything is missing. Run it once after you first point this at
production.

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
| `GET /admin/status` | pool snapshot |
| `GET /admin/usage` | bucketed usage history for the charts |
| `POST /admin/accounts` · `PATCH` · `DELETE` · `POST /admin/accounts/{id}/test` | manage credentials at runtime |
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

![The Keys screen: a form minting a key with pool, rpm, spend cap, priority and admin
flag, above a table of existing keys marked "in config" with their pools and
limits](docs/images/console-keys.png)

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
| `oauth` | A Claude subscription (Max/Pro). Three token sources, offered in the console's Add-account dialog: this machine's Claude Code login (nothing to configure), a long-lived token from `claude setup-token`, or an OAuth login the gateway runs itself — that last one stays disabled until you supply the provider endpoints, which this project will not guess at. Read [docs/oauth.md](docs/oauth.md) first: a subscription reports rolling-window utilisation rather than per-window budgets, so it keeps headroom routing and failover but has no leases, no admission control and no capacity horizon. |

![The Add-account dialog for a Claude subscription: three token sources — this
machine's Claude Code login, a long-lived token, or an OAuth login — with the machine
scan listing each credentials file it found, its plan, and whether it is current or
stale](docs/images/console-subscription-session.png)

All four sit in one pool, so a request can fail over from an API key to Bedrock. Use
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

Costs are reported, and spend caps enforced, only for models that have a price.
`pricing: builtin` takes the dated table that ships with this release — the console
shows its date beside every cost — and naming a model under `pricing:` overrides it.

Prices live in a dated data file rather than in code, because the gateway must never
bill you against a number nobody can attribute. A dated file whose date is on screen
can be attributed; a dict compiled into a release cannot.

### Privacy and security

- Prompt bodies are **never logged**. Only accounting: ids, accounts, tokens, latency.
- Credentials are never logged and are masked in every admin response.
- Binds to loopback. Refuses to start on a public interface without both
  `server.allow_remote: true` and at least one configured key.

---

## Documentation

Full docs build from `docs/` with `mkdocs serve`. Start with
[Why is my bill higher?](docs/caching.md) — it is the page that changes how you
configure this thing.

## Status

All eight milestones in `PLAN.md` are implemented and tested; see `TODO.md` for the
table and for what was deliberately left out.

Working today: passthrough and streaming, multi-account pooling, the error taxonomy,
retry and failover, the rate-limit mirror, token estimation and leases, headroom scoring,
circuit breakers, session affinity and cache accounting, admission control and a bounded
priority queue, virtual keys with model/pool/rpm/spend scoping,
structured logs, config hot reload, the admin API, and the CLI. Request priority with a
per-request wait budget, a batch spill lane, and SQLite-backed persistence for affinity
and windowed spend, a Redis store for multi-instance deployments, and runtime key
management behind an admin boundary, plus Bedrock and Vertex adapters and pluggable
routing strategies, and the operator console. 204 tests including a lease-concurrency
suite, a reproducible benchmark, and an end-to-end smoke test over real sockets
(`scripts/smoke.sh`).

All eight milestones in `PLAN.md` are built. `docs/UI-DESIGN.md` is the console's design
brief, and the console follows it.

**Credential types.** Anthropic API keys are the supported path. A *subscription*
account works — routing your own subscription through your own local gateway is the
ordinary case, and it reports enough (rolling-window utilisation) to route on — but
pooling several so their limits add up runs against Anthropic's consumer terms. Leases,
admission control and the capacity horizon need absolute token counts and stay dark for
those accounts. See [docs/oauth.md](docs/oauth.md).

## License

Apache-2.0.
