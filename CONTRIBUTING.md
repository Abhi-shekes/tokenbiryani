# Contributing

```bash
cd tokenbiryani
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,secrets]"
pytest                 # 379 tests
bash scripts/smoke.sh  # end-to-end over real sockets
```

Or bring the whole thing up in containers — gateway, Redis and a mock Anthropic —
with the source bind-mounted so edits reload:

```bash
docker compose up -d          # then http://localhost:8787/console
docker compose exec gateway pytest -q
docker compose down
```

`scripts/dev.sh` is the same stack without Docker; it writes a mock-backed
`tokenbiryani.yaml` on first run.

## The mock upstream is the point

`tests/support/mock_upstream.py` is a scriptable fake Anthropic API. Every routing
behaviour in this project is a reaction to something an upstream did — a 429 with a
`retry-after`, a 529, a stream that dies mid-flight, a limit header counting down — and
none of that is testable against the real API without spending money and waiting on real
reset windows.

```python
mock.script("acct-01", rate_limit(retry_after=30), overloaded(), ok())
mock.script("acct-02", stream_disconnect(after_chunks=3))
mock.accounts["acct-01"].exhaust()
```

It lives under `tests/` rather than in the package: it is scaffolding for this suite,
and shipping it would put a runnable fake Anthropic on every user's machine. The suite
imports it as `support.mock_upstream`; outside the suite, put `tests/` on the path.

It also runs as a real server for smoke tests:

```bash
PYTHONPATH=tests python -m support.server --port 9911 --accounts key-a,key-b
```

## The benchmark

`benchmarks/cache_affinity.py` is the evidence for the project's central claim, and
`tests/test_benchmark.py` fails if sticky routing ever stops beating cache-blind
routing. A change to the router that improves throughput while quietly costing cache
hits is a regression here, which is the point.

## The console

`tests/test_console.py` asserts on the served HTML; `tests/test_console_ui.py` drives
it in a real browser and is the only thing that can catch a panel that renders empty.
It skips itself when no Chrome or Chromium is installed, so it will pass silently on a
machine that cannot run it — check CI.

Screenshot-worthy states to keep working: no accounts (onboarding), the whole pool
cooling, a disabled credential, a rejected key, and a gateway that has stopped
answering — the last two are easy to break and invisible until someone hits them.

## Concurrency

`tests/test_concurrency.py` is the only place overlapping requests are exercised, and
leases are the mechanism it guards: without an atomic reservation, simultaneous
requests all read the same "plenty of headroom" and stampede one account into a 429.
`mock.latency` forces real overlap, and one test exists purely to prove the others
actually overlapped — a concurrency test that silently serialises proves nothing.

## State stores

`memory`, `sqlite` and `redis` are held to identical behaviour by one parametrised
suite in `tests/test_store.py` — add a backend there and it inherits every test. The
Redis backend runs against `fakeredis` by default; set `TOKENBIRYANI_REDIS_URL` to run
the same store against a real server, which is where range-bound semantics differ.

## Good first issues

Both live behind clean interfaces with the mock available to test against:

- **A routing strategy.** Add a `StrategySpec` to `core/router.py`. Scoring already
  produces per-candidate terms for the inspector, so a new strategy is mostly weights.
  A strategy can also ship in its own package — publish it under the
  `tokenbiryani.strategies` entry point group and `tokenbiryani strategies` will list
  it. Publish either a `StrategySpec` (reweights the built-in terms) or an object with
  a `terms()` method (computes its own). A plugin that fails to import is logged and
  skipped, never fatal.
- **A provider adapter.** Implement `providers.base.Upstream` — `url`, `auth_headers`,
  and optionally override `send`/`open_stream`, plus `iter_sse` if the platform does
  not stream SSE natively. `providers/bedrock.py` is the awkward case worth reading:
  SigV4 per request and a binary event-stream decoded back to SSE.

## Branches

`main` is the trunk and the only long-lived branch. It is protected: no direct
pushes, no force-pushes, no deletion, and every commit on it arrived through a pull
request whose checks were green. History is linear — merges are squashes, so `main`
reads as one commit per change rather than a braid.

Work happens on a short-lived branch off `main`, named for what it does:

| Prefix | For |
|---|---|
| `feat/` | a new capability |
| `fix/` | a defect with a reproduction |
| `docs/` | documentation only |
| `chore/` | dependencies, CI, tooling, release mechanics |
| `refactor/` | behaviour-preserving change, with the tests to prove it |

```bash
git switch -c fix/lease-released-twice main
# ... work, commit ...
git push -u origin fix/lease-released-twice
gh pr create --fill
```

Keep them short. A branch that lives a week is a merge conflict with someone else's
week, and this project's files — `core/gateway.py`, `console.html` — are exactly the
ones two branches both want.

### Commit messages

A subject line in the imperative that says what changed, then a body that says what
was wrong. The body is the valuable half: the log is the only place the reasoning
survives, and "fix bug" in six months is a mystery. `git log` shows the standard.

## Changelog

`CHANGELOG.md` is written as you go, not reconstructed at release time. Every pull
request that changes something a user would notice adds a line under
`## [Unreleased]`, and CI fails the PR if it does not. If the change genuinely is
invisible — a test, a refactor, a typo — apply the **`no-changelog`** label and the
check stands down.

Write for someone upgrading, not for the reviewer. "Fixed a bug in the router" tells
them nothing; "a 400 from one account no longer retries across the whole pool" tells
them whether they were affected.

## Releasing

The tag is the release. Everything else — PyPI, the GHCR image, the docs site, the
GitHub release and its notes — is produced by `.github/workflows/release.yml` from
that tag, so there is no manual publishing step to get wrong or to forget.

1. Move `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` in `CHANGELOG.md`, and open a
   fresh empty `## [Unreleased]` above it.
2. Bump the version in **both** `pyproject.toml` and `src/tokenbiryani/__init__.py`.
3. Merge that through a pull request like anything else.
4. Tag the merge commit and push it:

   ```bash
   git switch main && git pull
   git tag -a v0.2.0 -m "tokenbiryani 0.2.0"
   git push origin v0.2.0
   ```

The workflow refuses the release unless the tag, `pyproject.toml` and `__init__.py`
all name the same version and `CHANGELOG.md` has a dated section for it — the three
ways a release usually ends up describing a version it is not. Check what it will
say before you tag:

```bash
python scripts/changelog.py check v0.2.0     # the guard the workflow runs
python scripts/changelog.py extract v0.2.0   # the notes it will publish
```

PyPI publishing is gated on the repository variable `PYPI_PUBLISH` so that tags do
not fail while no trusted publisher exists. Turn it on once one does:

```bash
gh variable set PYPI_PUBLISH --body true
```

## House rules

- **The gateway is a router, not a rewriter.** Nothing may modify a request body. This is
  what keeps it correct across API changes; a PR that parses and rebuilds a request will
  be asked to stop.
- **All shared state goes through `StateStore`.** Reaching around it into a dict is the
  one thing that would make the multi-instance backend a rewrite instead of an
  implementation.
- **New failure modes go in the taxonomy**, not in an `if` at the call site. Account
  action and request action are separate decisions on purpose.
- **Never log prompt bodies** on a default path.
- Tests for behaviour, not for implementation detail. If a test needs a deterministic
  first pick, use `strategy="priority"` rather than asserting on the tie-break hash.

Docs live in `docs/` and build with `pip install -e ".[docs]" && mkdocs serve`. CI
builds them with `--strict`, so a broken internal link fails the build.

Before pushing: `ruff check .`, `mypy`, and `pytest`. CI additionally builds the
image and boots it, runs the store suite against a real Redis, and gates coverage
at 80%.

The type check is deliberately not strict. It exists to catch `Optional` handling
that would crash at runtime, not to make you annotate every local.
