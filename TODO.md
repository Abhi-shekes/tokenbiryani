# Build status

All eight milestones in [PLAN.md](PLAN.md) are implemented and tested, plus the
console rework described below and the onboarding work in
[docs/UX-PLAN.md](docs/UX-PLAN.md).

`pytest` = 341 passed, 1 skipped (the skip runs against a real Redis when
`TOKENBIRYANI_REDIS_URL` is set). `ruff` and `mypy` clean. The smoke test
passes over real sockets, the container image builds and boots, the docs site builds
with `--strict`, and `benchmarks/cache_affinity.py` reproduces the routing claim.

| | Milestone | Status |
|---|---|---|
| M0 | Spike — passthrough, streaming | done |
| M1 | Pool — failover, error taxonomy, mock harness, CLI | done |
| M2 | Smart routing — limit mirror, leases, headroom, breakers | done |
| M3 | Continuity — affinity, fingerprinting, cache accounting | done |
| M4 | Queue — admission, priority, deadlines, batch spill lane | done |
| M5 | Observability — logs, admin API, operator console | done |
| M6 | Multi-tenant — virtual keys, spend caps, SQLite + Redis stores | done |
| M7 | Breadth — Bedrock and Vertex adapters, strategy plugins | done |
| M8 | Console — account lifecycle in the UI, usage history, charts | done |
| M9 | Subscription login — OAuth + PKCE, session refresh | done, unverified |
| M10 | Onboarding — first run, account configuration, prices | done |

## What is left

**Nothing here has ever talked to the real Anthropic API.** Every test and the smoke
run go through `tests/support/mock_upstream.py`, which encodes assumptions about
header spellings, error body shapes and the Batches API contract. If
`anthropic-ratelimit-input-tokens-remaining` is named differently in practice, the
mirror stays empty, every account reads as full, and the router silently degrades to
round-robin — shredding the prompt cache while looking healthy.

`tokenbiryani doctor` exists to settle exactly this. It sends one real request
(`max_tokens=1`) and prints the headers the upstream actually returned next to the nine
the limit mirror looks for, plus what the mirror parsed out of them:

```bash
tokenbiryani doctor --api-key sk-ant-...
```

It exits non-zero if any header is missing or spelled differently. **One real API key
and one run of that command closes this item.** It is still the most important one.

M10 made it much harder to *not* run. The same check — one implementation, in
`core/diagnostics.py` — is now step 2 of the console's onboarding wizard and the body
of `tokenbiryani accounts test`, both riding on a `GET /v1/models` probe rather than a
paid completion. So the first account anyone adds is checked, by default, without
anyone having heard of the command.

**The OAuth endpoints are unverified too, and deliberately unset.** The login the
gateway runs itself is implemented, tested against a scripted token endpoint, and
refuses to run until `oauth.client_id`, `oauth.authorize_url` and `oauth.token_url` are
configured. Anthropic does not publish the values its first-party clients use, and
guessing would ship a feature that looks supported and fails inexplicably. See
[docs/oauth.md](docs/oauth.md) and
[ADR-0004](docs/adr/0004-subscription-login-in-core.md).

The rest of `type: oauth` is no longer unverified. Reading this machine's Claude Code
credentials file and pasting a `claude setup-token` token have both been run against a
live Pro subscription end to end — `anthropic-beta: oauth-2025-04-20` accepted,
streaming and non-streaming, `GET /v1/models` probe answering — and the rolling-window
headers the mirror routes on (`anthropic-ratelimit-unified-*`) were read off real
responses. What is still untested against the real thing is the authorization-code
exchange itself, which is the part those three settings gate.

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
- **No price list compiled into the code.** As of M10 a *dated* table ships as data
  (`src/tokenbiryani/prices.yaml`), opted into with `pricing: builtin` and overridable
  model by model. The console shows its `as_of` date beside every cost. The rule that
  produced the original exclusion is unchanged and is what the date is for: the gateway
  must never bill against a number nobody can attribute. Without an opt-in, costs still
  read `—` — and the Usage screen now says why instead of drawing an empty chart.
