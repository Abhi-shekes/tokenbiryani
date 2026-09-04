# Contributing

```bash
git clone https://github.com/OWNER/tokenbiryani && cd tokenbiryani
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                 # 86 tests, ~1.5s
bash scripts/smoke.sh  # end-to-end over real sockets
```

## The mock upstream is the point

`tokenbiryani.testing.mock_upstream` is a scriptable fake Anthropic API. Every routing
behaviour in this project is a reaction to something an upstream did — a 429 with a
`retry-after`, a 529, a stream that dies mid-flight, a limit header counting down — and
none of that is testable against the real API without spending money and waiting on real
reset windows.

```python
mock.script("acct-01", rate_limit(retry_after=30), overloaded(), ok())
mock.script("acct-02", stream_disconnect(after_chunks=3))
mock.accounts["acct-01"].exhaust()
```

It also runs as a real server for smoke tests:

```bash
python -m tokenbiryani.testing.server --port 9911 --accounts key-a,key-b
```

## The benchmark

`benchmarks/cache_affinity.py` is the evidence for the project's central claim, and
`tests/test_benchmark.py` fails if sticky routing ever stops beating cache-blind
routing. A change to the router that improves throughput while quietly costing cache
hits is a regression here, which is the point.

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

Before pushing: `ruff check .`, `mypy`, and `pytest`. CI additionally builds the
image and boots it, runs the store suite against a real Redis, and gates coverage
at 80%.

The type check is deliberately not strict. It exists to catch `Optional` handling
that would crash at runtime, not to make you annotate every local.
