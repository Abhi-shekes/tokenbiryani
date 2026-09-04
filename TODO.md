# Build TODO — M0 through M3

All complete. `pytest` = 86 passed; `bash scripts/smoke.sh` = all checks passed.

- [x] 01 Repo scaffolding: pyproject, LICENSE, gitignore, package skeleton
- [x] 02 Config + Account model + registry (tokenbiryani.yaml, env interpolation)
- [x] 03 Error taxonomy
- [x] 04 Rate-limit mirror, token estimation, atomic leases
- [x] 05 Health state machine + circuit breaker
- [x] 06 StateStore interface + in-memory implementation
- [x] 07 Session affinity + prefix fingerprinting
- [x] 08 Router: strategy protocol + 5 strategies
- [x] 09 Admission control + priority wait queue
- [x] 10 Upstream protocol + Anthropic API adapter
- [x] 11 Passthrough proxy: first-token buffering + transparent retry
- [x] 12 Virtual keys + auth
- [x] 13 FastAPI app: /v1/messages, /v1/models, count_tokens, /healthz, /metrics, /admin/*
- [x] 14 Observability: event ring buffer, metrics, structured logs
- [x] 15 CLI: init / serve / status
- [x] 16 Mock Anthropic upstream harness
- [x] 17 Test suite against the mock
- [x] 18 Full suite green
- [x] 19 End-to-end smoke test
- [x] 20 Docs (README, CONTRIBUTING, SECURITY, ADRs) + CI
