# Build status

All eight milestones in [PLAN.md](PLAN.md) are implemented and tested.

`pytest` = 203 passed, 1 skipped (the skip runs against a real Redis when
`TOKENBIRYANI_REDIS_URL` is set), 85% coverage. `ruff` and `mypy` clean. The smoke test
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

## What is left

**Nothing here has ever talked to the real Anthropic API.** Every test and the smoke
run go through `tokenbiryani.testing.mock_upstream`, which encodes assumptions about
header spellings, error body shapes and the Batches API contract. If
`anthropic-ratelimit-input-tokens-remaining` is named differently in practice, the
mirror stays empty, every account reads as full, and the router silently degrades to
round-robin — shredding the prompt cache while looking healthy. One real API key would
settle it. This is the only outstanding item, and it is the important one.

Smaller, and genuinely optional:

- No OpenTelemetry traces (PLAN §8 lists them as optional).
- Request history is memory-only. Even on SQLite, a restart loses the event log, so
  the console's account detail can only show current values rather than the limit
  windows over time that `docs/UI-DESIGN.md` describes.

## Not built, and deliberately

- **No OAuth adapter in core.** One exists as a separate distribution at
  `contrib/tokenbiryani-oauth/`, which core neither depends on nor installs. See
  [ADR-0003](docs/adr/0003-api-keys-only.md) and that package's README, which is
  blunt about what it costs you.
- **Config is read-only over HTTP.** `POST /admin/reload` re-reads the file; the API
  will not write it. Keys are the exception, because they are credentials rather than
  configuration.
- **No price list.** Costs are reported only for models named under `pricing:`.
