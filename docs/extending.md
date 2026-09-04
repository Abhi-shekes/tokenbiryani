# Extending

Three interfaces carry the extensibility story. Each has a test suite you inherit.

## A routing strategy

Ship it in your own package under the `tokenbiryani.strategies` entry point:

```toml
[project.entry-points."tokenbiryani.strategies"]
my_strategy = "my_package:MY_STRATEGY"
```

Publish either a `StrategySpec`, which reweights the built-in terms:

```python
from tokenbiryani.core.router import StrategySpec

MY_STRATEGY = StrategySpec(affinity=1.0, headroom=2.0, cost=4.0)
```

or an object with a `terms()` method, which computes its own. Those terms show up per
candidate in the request inspector, so name them for a reader:

```python
class PreferWarmest:
    def terms(self, account, ctx, weights, index, count):
        return {"cache_rate": account.cache_hit_rate() or 0.0}
```

A plugin that fails to import is logged and skipped — a broken third-party package
must not stop the gateway starting. `tokenbiryani strategies` lists what loaded.

## A provider adapter

Ship it under the `tokenbiryani.providers` entry point, and the account `type` becomes
available without touching core:

```toml
[project.entry-points."tokenbiryani.providers"]
my_platform = "my_package:MyUpstream"
```

The value is anything callable with an `AccountConfig` that returns an `Upstream`.
Built-in type names cannot be shadowed, and a plugin that fails to import is logged
and skipped.

Implement `providers.base.Upstream`: `url`, `auth_headers`, and optionally override
`send` / `open_stream`. If the platform does not stream SSE natively, override
`iter_sse` and translate there, so the rest of the gateway keeps reading ordinary
streaming.

`providers/bedrock.py` is the awkward case worth reading: SigV4 per request, and a
binary event-stream decoded back to SSE.

If your platform reports no `anthropic-ratelimit-*` headers, set
`observable_limits: false` on those accounts. Otherwise their windows never populate,
an unknown window reads as full, and the account beats every account that honestly
reports a partly-used budget — permanently. Unobservable accounts are scored at
`assumed_headroom` and excluded from the capacity horizon, which would otherwise be
promising capacity nobody can see.

## A state store

Implement `store.base.StateStore` and add it to `build_store`. The memory, SQLite and
Redis backends are held to identical behaviour by one parametrised suite in
`tests/test_store.py`, so a new backend inherits every test by adding one fixture
parameter.

Watch the range semantics: Redis `ZREMRANGEBYSCORE` is inclusive at the upper bound,
which made it evict one entry more than the others until the parity suite caught it.

## Testing your extension

The package ships a scriptable fake Anthropic API:

```python
from tokenbiryani.testing.mock_upstream import MockAnthropic, rate_limit, ok

mock = MockAnthropic()
mock.add("acct-01", "key-a", cache_aware=True)
mock.script("acct-01", rate_limit(retry_after=30), ok())
mock.latency = 0.05     # force real concurrency overlap
```

It also runs as a real server for end-to-end work:

```bash
python -m tokenbiryani.testing.server --port 9911 --accounts key-a,key-b
```
