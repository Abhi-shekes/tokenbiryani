# Build status

All eight milestones in [PLAN.md](PLAN.md) are implemented and tested.

`pytest` = 189 passed, 1 skipped (the skipped one runs against a real Redis when
`TOKENBIRYANI_REDIS_URL` is set). `ruff check .` clean. `bash scripts/smoke.sh` passes
end to end over real sockets.

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

## Not built, and deliberately

- **No OAuth / subscription-account adapter.** See [ADR-0003](docs/adr/0003-api-keys-only.md).
- **Config is read-only over HTTP.** `POST /admin/reload` re-reads the file; the API
  will not write it. Keys are the exception, because they are credentials rather than
  configuration.
- **No price list.** Costs are reported only for models named under `pricing:`.
