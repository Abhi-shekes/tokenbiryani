# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

**Gateway**
- Anthropic-compatible endpoint: `POST /v1/messages` (streaming and not), with
  `count_tokens` and `models` passed through.
- Multi-account pooling with a live `anthropic-ratelimit-*` mirror, token estimation
  and atomic leases.
- Six routing strategies; `sticky_headroom` is the default. Third-party strategies load
  from the `tokenbiryani.strategies` entry point group.
- Error taxonomy separating account action from request action.
- Transparent streaming failover up to the first content delta, and an SSE `error`
  frame after it.
- Session affinity via stable-prefix fingerprinting, with cache-break accounting.
- Admission control and a bounded priority wait queue; `X-TokenBiryani-Priority` and
  `X-TokenBiryani-Max-Wait`, with per-key defaults.
- Batch spill lane: saturated batch-priority requests go to the Message Batches API,
  bounded by their own wait budget, and fall back to the queue if it is unavailable.

**Upstreams**
- `anthropic_api`, `bedrock` (SigV4, AWS event-stream decoded back to SSE) and
  `vertex` (bearer token, native SSE). All three share one pool.

**State**
- `StateStore` with `memory`, `sqlite` and `redis` backends, held to identical
  behaviour by one parametrised suite.
- Spend is a windowed ledger rather than a lifetime total, and survives a restart.

**Console**
- Account lifecycle in the UI: add, name, test, rename, rotate, enable, disable and
  delete accounts of any type without touching the config file. Accounts declared in
  `tokenbiryani.yaml` render locked — the file stays the operator's.
- Add-account form per credential type: Anthropic API key, Claude subscription,
  Bedrock, Vertex.
- Usage screen: persisted history over 1h / 24h / 7d / 30d, grouped by account, model
  or key, with token, cost and cache-hit charts, a totals table and hover detail.
- "Connect a client" screen with the exact export lines for this gateway's address.
- First-run wizard replaces the copy-this-YAML empty state.
- Rebuilt on a sidebar shell; the stylesheet is served from `/console.css`.

**Usage history**
- `usage_events`: one row per request — tokens, cache split, model, key, status,
  latency, cost — kept 90 days on `sqlite` and `redis`, bounded in `memory`. Charts
  now survive a restart.
- `GET /admin/usage`, bucketed and grouped, with one shared aggregator so the three
  store backends cannot disagree.

**Subscription accounts**
- `type: oauth` in core, with an OAuth 2.0 + PKCE login driven from the console and
  background session refresh. Ships inert: the provider endpoints are unset by
  default and the flow says which are missing. See ADR-0004 and docs/oauth.md.

**Operations**
- `tokenbiryani doctor`: sends one real request and reports the rate-limit headers the
  upstream actually returned against the ones the router expects.
- `scripts/dev.sh` brings up the mock upstream and the gateway together.
- Virtual keys with model, pool, rpm, spend and priority scoping; runtime minting and
  revocation, stored hashed, behind an `admin: true` boundary.
- Prometheus metrics, structured logs that never contain prompts, and an admin API
  with a per-request routing inspector.
- Config hot reload that preserves the health of accounts that survive it.
- CLI: `init`, `serve`, `status`, `strategies`, `keygen`, `doctor`.

**Testing**
- Scriptable mock Anthropic upstream, usable in-process or as a real server, which
  models the per-credential prompt cache and can add latency to force real overlap.
- 203 tests, including a lease-concurrency suite, at 85% coverage.
- `benchmarks/cache_affinity.py`, with a regression test that fails if sticky routing
  ever stops beating cache-blind routing.
- End-to-end smoke test over real sockets.

### Deprecated
- `contrib/tokenbiryani-oauth`. `type: oauth` is a built-in account type now, and a
  plugin may not shadow a built-in, so the entry point is gone. Core absorbed the
  package's token sources with identical option names, so existing configuration keeps
  working; what remains is a re-export shim that warns on import.

**Project**
- Container image (non-root, healthchecked) and `docker compose up`.
- CI: ruff, mypy, coverage-gated tests on 3.8/3.10/3.12, a real-Redis job, an image
  boot check, a strict docs build, and release-on-tag to PyPI and GHCR.
- Documentation site, code of conduct, issue and PR templates.

### Fixed
- Re-enabling an account now clears its `disabled_reason` and resets its breaker.
  Previously the toggle read "on" while the account stayed out of the pool.
- `/admin/status` refreshes managed accounts, so an account added elsewhere appears
  without a restart.
- `GET /admin/accounts/{id}` returns the account's name, source and configuration;
  it previously returned only live runtime state, so the console could not show what
  it was called.
- SQLite WAL sidecars (`*.db-shm`, `*.db-wal`) are no longer tracked by git.

### Security
- Subscription access and refresh tokens are encrypted at rest and stripped by name
  from every admin API payload.
- `/admin/*` requires an admin key. Previously any valid key could read the pool's
  account ids and spend.
- Managed keys are stored as a hash; keys too short to mask safely are hidden entirely.
- The server refuses a non-loopback bind without both `allow_remote` and a configured key.
