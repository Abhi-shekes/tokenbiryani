# Build status

All eight milestones in [PLAN.md](PLAN.md) are implemented and tested, plus the
console rework described below.

`pytest` = 296 passed, 1 skipped (the skip runs against a real Redis when
`TOKENBIRYANI_REDIS_URL` is set), 87% coverage. `ruff` and `mypy` clean. The smoke test
passes over real sockets, the container image builds and boots, the docs site builds
with `--strict`, and `benchmarks/cache_affinity.py` reproduces the routing claim.

| | Milestone | Status |
|---|---|---|
| M0 | Spike — passthrough, streaming | done |
| M1 | Pool — failover, error taxonomy, mock harness, CLI | done |
| M2 | Smart routing — limit mirror, leases, headroom, breakers | done |
| M3 | Continuity — affinity, fingerprinting, cache accounting | done |
| M4 | Queue — admission, priority, deadlines, batch spill lane | done |
| M5 | Observability — metrics, logs, admin API, operator console | done |
| M6 | Multi-tenant — virtual keys, spend caps, SQLite + Redis stores | done |
| M7 | Breadth — Bedrock and Vertex adapters, strategy plugins | done |
| M8 | Console — account lifecycle in the UI, usage history, charts | done |
| M9 | Subscription login — OAuth + PKCE, session refresh | done, unverified |

## What is left

**Nothing here has ever talked to the real Anthropic API.** Every test and the smoke
run go through `tokenbiryani.testing.mock_upstream`, which encodes assumptions about
header spellings, error body shapes and the Batches API contract. If
`anthropic-ratelimit-input-tokens-remaining` is named differently in practice, the
mirror stays empty, every account reads as full, and the router silently degrades to
round-robin — shredding the prompt cache while looking healthy.

`tokenbiryani doctor` now exists to settle exactly this. It sends one real request
(`max_tokens=1`) and prints the headers the upstream actually returned next to the nine
the limit mirror looks for, plus what the mirror parsed out of them:

```bash
tokenbiryani doctor --api-key sk-ant-...
```

It exits non-zero if any header is missing or spelled differently. **One real API key
and one run of that command closes this item.** It is still the most important one.

**The OAuth endpoints are unverified too, and deliberately unset.** Subscription login
is implemented, tested against a scripted token endpoint, and refuses to run until
`oauth.client_id`, `oauth.authorize_url` and `oauth.token_url` are configured. Anthropic
does not publish the values its first-party clients use, and guessing would ship a
feature that looks supported and fails inexplicably. See [docs/oauth.md](docs/oauth.md)
and [ADR-0004](docs/adr/0004-subscription-login-in-core.md). Also unverified: the
`anthropic-beta` value a subscription session expects.

Smaller, and genuinely optional:

- No OpenTelemetry traces (PLAN §8 lists them as optional).
- The `memory` store keeps usage history in a 50,000-row buffer that dies with the
  process. `sqlite` and `redis` persist 90 days; that is the difference between the
  charts meaning something tomorrow and not.

## Not built, and deliberately

- **Config is read-only over HTTP.** `POST /admin/reload` re-reads the file; the API
  will not write it. Accounts and keys are the exception, because they are credentials
  rather than configuration — and accounts added through the console live in the store,
  not the file. An account declared in `tokenbiryani.yaml` stays the file's: the API
  returns 409 rather than editing it.
- **No price list.** Costs are reported, and spend caps enforced, only for models named
  under `pricing:`.
