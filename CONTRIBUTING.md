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

## Good first issues

Both live behind clean interfaces with the mock available to test against:

- **A routing strategy.** Add a `StrategySpec` to `core/router.py`. Scoring already
  produces per-candidate terms for the inspector, so a new strategy is mostly weights.
- **A provider adapter.** Implement `providers.base.Upstream` — `url`, `auth_headers`,
  and optionally override `send`/`open_stream`. Bedrock and Vertex are open.

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

Run `ruff check .` before pushing.
