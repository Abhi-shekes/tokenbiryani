# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-09-05

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
- Operator settings persist across restarts (`put_setting`/`get_settings`), so a
  strategy chosen in the console is still in force after a deploy.
- Spend is a windowed ledger rather than a lifetime total, and survives a restart.

**Console**
- Account lifecycle in the UI: add, name, test, rename, rotate, enable, disable and
  delete accounts of any type without touching the config file.
- Accounts declared in `tokenbiryani.yaml` can be operated from the console without
  being overwritten by it. The file keeps the credential, the type and the base URL;
  `enabled`, `name`, `cost_tier`, `priority`, `spend_cap_usd`, `models` and
  `max_concurrency` can be changed here, are stored in the gateway rather than
  written back to the file, survive a reload, and are undone in one step
  (`DELETE /admin/accounts/{id}/override`). Turning an account off no longer requires
  an editor on the server.
- Keys can be rescoped in place (`PATCH /admin/keys/{name}`) instead of revoked and
  reissued, which breaks every client holding them. Nullable limits can be cleared.
  The common case is a key scoped at a since-deleted account, which blocks reload.
- Routing strategy and price table are editable on the Settings screen. Both are
  stored in the state store, re-applied after every reload — so an unrelated edit to
  the YAML cannot silently revert them — and tagged with where the current value came
  from, the file or the console.
- Add-account form per credential type: Anthropic API key, Claude subscription,
  Bedrock, Vertex.
- Usage screen: persisted history over 1h / 24h / 7d / 30d, grouped by account, model
  or key, with token, cost and cache-hit charts, a totals table and hover detail.
- "Connect a client" screen with the exact export lines for this gateway's address.
- A landing page at `/console` for anyone not signed in: what the gateway is, the
  endpoint to point clients at, and the admin-key field, rather than a bare box.
- A drawn mark — a sealed, layered pot — replacing the emoji, legible down to a 16px
  favicon where the emoji was not.
- First-run wizard replaces the copy-this-YAML empty state: a full page with a
  four-step rail, and each step does its own work rather than opening a dialog.
  Step 1 offers the credential routes as cards and leads with the Claude Code
  logins found on this machine; step 2 mounts the account form inline and names a
  subscription account after the profile directory it reads. Reachable again from
  **Add an account** in the sidebar, not only on an empty pool.
- Rebuilt on a sidebar shell; the stylesheet is served from `/console.css`.

### Removed

**Console**
- Row density toggle. One control, one stored preference and a second spacing scale
  to keep working, for a setting nobody was reaching for.

**Usage history**
- `usage_events`: one row per request — tokens, cache split, model, key, status,
  latency, cost — kept 90 days on `sqlite` and `redis`, bounded in `memory`. Charts
  now survive a restart.
- `GET /admin/usage`, bucketed and grouped, with one shared aggregator so the three
  store backends cannot disagree.

**Subscription accounts**
- `type: oauth` in core, with an OAuth 2.0 + PKCE login driven from the console and
  background session refresh. That login ships inert: the provider endpoints are
  unset by default and the flow says which are missing. See ADR-0004 and
  docs/oauth.md.
- Two token sources that need nothing configured, offered beside it in the
  Add-account dialog: **this machine's Claude Code login**, read from the CLI's
  credentials file and re-read whenever the CLI refreshes it, and **a long-lived
  token** from `claude setup-token`, encrypted at rest like any other credential.
  A subscription is now something you can add from the console on a fresh install.
- `GET /admin/oauth/detect` scans for Claude Code logins and reports path,
  subscription type and expiry — never the token. It covers `$CLAUDE_CONFIG_DIR`,
  `~/.claude`, every `~/.claude-*` profile directory and `~/.config/claude`, which
  is how two accounts actually live on one machine: a config directory each, picked
  by a shell alias. It exists because `CLAUDE_CONFIG_DIR` makes
  `~/.claude/.credentials.json` a *different account* whose token may have expired
  weeks ago, and the 401 that follows names neither file.
- Rolling-window rate limits. A subscription reports
  `anthropic-ratelimit-unified-{5h,7d}-{status,utilization,reset}` rather than the
  per-window triples an API key sends, and the mirror now reads them: these accounts
  route on `1 - utilization` instead of a fixed `assumed_headroom` guess, refuse when
  a window says `rejected`, count down to a real reset, and draw **5h**/**7d** meters
  in the console. Leases, admission control and the capacity horizon still need
  absolute token counts and remain off for them. `doctor` and the console's verify
  step check the unified headers for these accounts rather than reporting nine
  missing ones on a healthy account.

**Operations**
- `tokenbiryani doctor`: sends one real request and reports the rate-limit headers the
  upstream actually returned against the ones the router expects.
- `scripts/dev.sh` brings up the mock upstream and the gateway together.
- Virtual keys with model, pool, rpm, spend and priority scoping; runtime minting and
  revocation, stored hashed, behind an `admin: true` boundary.
- Structured logs that never contain prompts, and an admin API with a per-request
  routing inspector.
- Config hot reload that preserves the health of accounts that survive it.
- CLI: `init`, `serve`, `status`, `strategies`, `keygen`, `doctor`.

**Testing**
- Scriptable mock Anthropic upstream, usable in-process or as a real server, which
  models the per-credential prompt cache and can add latency to force real overlap.
- 378 tests, including a lease-concurrency suite and browser tests driving the
  console through Playwright, at 85% coverage.
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
- The bundled price table is keyed by prefix (`claude-sonnet-5*`). Its keys were exact
  model ids, and the API answers with dated ones (`claude-haiku-4-5-20251001`), so
  every request priced as `null` and the cost column stayed empty on a correctly
  configured gateway.
- The Settings screen no longer discards a choice made while it was still loading. It
  repaints when the load returns, and read its values from the DOM at save time — so a
  dropdown changed in that window saved the old value and reported success.
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
- The browser test suite no longer writes a credential-encryption key into the
  working directory. `store.secret_key_path` defaults relative to the working
  directory, so adding an account through the console under test dropped a 0600
  secret next to the source and reused it on every later run. A session fixture now
  fails the run if any test does this again.

### Security
- Subscription access and refresh tokens are encrypted at rest and stripped by name
  from every admin API payload.
- `/admin/*` requires an admin key. Previously any valid key could read the pool's
  account ids and spend.
- Managed keys are stored as a hash; keys too short to mask safely are hidden entirely.
- The server refuses a non-loopback bind without both `allow_remote` and a configured key.
