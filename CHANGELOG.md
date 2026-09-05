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
- A landing page at `/console` for anyone not signed in: what the gateway is, the
  endpoint to point clients at, and the admin-key field, rather than a bare box.
- A drawn mark — a sealed, layered pot — replacing the emoji, legible down to a 16px
  favicon where the emoji was not.
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

### Removed
- Dead `github.com/OWNER/...` links from `pyproject.toml`, `mkdocs.yml`, the issue
  template and the docs. This repository has no home yet, so each of those shipped a
  404 — including a "report a vulnerability privately" link that went nowhere.
- `contrib/tokenbiryani-oauth`. `type: oauth` is a built-in account type now, and a
  plugin may not shadow a built-in, so its entry point could no longer load. Core
  carries the same token sources under the same option names — `credentials_path`,
  `token_env`, `access_token` — so configuration written against it keeps working.
  The package was never published, so the shim protected no one and is gone.

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
- `docker/tokenbiryani.yaml` is in the repository. An unanchored `tokenbiryani.yaml`
  ignore rule matched it at every depth, so the file `docker compose up` mounts was
  never committed and a fresh clone could not start the stack.
- The compose stack demonstrates the thing the project is about. Its mock upstream
  now models the per-credential prompt cache (`--cache` on
  `tokenbiryani.testing.server`), and `docker/tokenbiryani.yaml` carries illustrative
  pricing — so cache hit rate and cost read as real numbers instead of a flat 0% and
  a column of $0.0000 on the gateway's own demo stack.
- `serve` bounds its graceful shutdown. This gateway always holds a connection that
  never ends — `/admin/events` is an SSE stream open for as long as a console tab is
  — so a reload or a restart hung at "Waiting for connections to close" with the port
  open and answering nothing. That wedged the container under `serve --reload`
  whenever a source file was edited with the console open.
- A rejected key costs one request instead of five, and says where the right key
  lives. Signing in used to start the poll and the event stream before knowing the
  key was any good, and the stream then retried the refused key on its own timer.
- `serve` prints the key names this gateway accepts, masked — enough to see that the
  key in your browser belongs to a different gateway, never enough to use one.
- The console now says when the gateway has stopped answering, instead of polling it
  forever while the live chip read "polling" and pre-outage numbers sat there looking
  current. It names the address, dims the stale panel, backs off to a 30s cap, offers
  a Retry, and reconnects the event stream as soon as the gateway is back.
- The console page and stylesheet are re-read when they change on disk. They were
  cached for the life of the process, so editing the console under `serve --reload`
  or with the source bind-mounted showed the old page until something restarted it.
- `.manual-test-key`, a credential-encryption key for a database that no longer
  exists, is no longer committed.

### Security
- Subscription access and refresh tokens are encrypted at rest and stripped by name
  from every admin API payload.
- `/admin/*` requires an admin key. Previously any valid key could read the pool's
  account ids and spend.
- Managed keys are stored as a hash; keys too short to mask safely are hidden entirely.
- The server refuses a non-loopback bind without both `allow_remote` and a configured key.
