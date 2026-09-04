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

**Operations**
- Virtual keys with model, pool, rpm, spend and priority scoping; runtime minting and
  revocation, stored hashed, behind an `admin: true` boundary.
- Prometheus metrics, structured logs that never contain prompts, and an admin API
  with a per-request routing inspector.
- Config hot reload that preserves the health of accounts that survive it.
- Operator console at `/console`: pool, capacity horizon, live request feed, routing
  inspector, account detail, key management.
- CLI: `init`, `serve`, `status`, `strategies`, `keygen`.

**Testing**
- Scriptable mock Anthropic upstream, usable in-process or as a real server, which
  models the per-credential prompt cache and can add latency to force real overlap.
- 203 tests, including a lease-concurrency suite, at 85% coverage.
- `benchmarks/cache_affinity.py`, with a regression test that fails if sticky routing
  ever stops beating cache-blind routing.
- End-to-end smoke test over real sockets.

**Contrib**
- `contrib/tokenbiryani-oauth`, a separate distribution adding `type: oauth` for
  subscription sessions. Not installed by `pip install tokenbiryani`.

**Project**
- Container image (non-root, healthchecked) and `docker compose up`.
- CI: ruff, mypy, coverage-gated tests on 3.8/3.10/3.12, a real-Redis job, an image
  boot check, a strict docs build, and release-on-tag to PyPI and GHCR.
- Documentation site, code of conduct, issue and PR templates.

### Security
- `/admin/*` requires an admin key. Previously any valid key could read the pool's
  account ids and spend.
- Managed keys are stored as a hash; keys too short to mask safely are hidden entirely.
- The server refuses a non-loopback bind without both `allow_remote` and a configured key.
