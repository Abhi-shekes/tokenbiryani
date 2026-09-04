# Deployment

## Docker

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export TOKENBIRYANI_KEY=$(docker compose run --rm --no-deps gateway keygen)
docker compose up
```

The image runs as a non-root user and carries a healthcheck wired to `/healthz`, which
reports unhealthy exactly when no account is ready. Its port is read from
`TOKENBIRYANI_HEALTH_PORT` so a config serving elsewhere is still checked correctly.

Binding `0.0.0.0` is the point of a container, so `docker/tokenbiryani.yaml` sets
`server.allow_remote: true` and defines a key. Without both, the gateway refuses to
start rather than expose your credentials to the network.

## More than one instance

```yaml
store:
  backend: redis
  url: redis://127.0.0.1:6379/0
  namespace: tokenbiryani
```

Shares affinity, the spend ledger and per-key rate counters between processes. Without
it, two instances each keep their own affinity map — so a conversation ping-pongs
between them and loses its cache — and each enforces its own half of every spend cap.

Needs `pip install "tokenbiryani[redis]"`.

## One instance that survives restarts

```yaml
store:
  backend: sqlite
  path: tokenbiryani.db
```

The default from `tokenbiryani init`. `memory` loses affinity and the spend ledger when
the process dies, which silently resets every cap.

## Security checklist

- Give clients **virtual keys**; real credentials never leave the gateway process.
- Mark exactly the keys that need it `admin: true`. `/admin` exposes account ids,
  spend and key management.
- Keep `log_bodies` off.
- Keep the config file readable only by the gateway's user: it contains real
  credentials by design.
- Do not put an admin key in a URL. The console reads its SSE feed through `fetch`
  rather than `EventSource` for exactly this reason.

## Metrics

Scrape `/metrics`. The series worth alerting on:

| | |
|---|---|
| `tokenbiryani_account_headroom` | per account, 0 means no budget left |
| `tokenbiryani_queue_depth` | sustained non-zero means the pool is undersized |
| `tokenbiryani_cache_breaks_total` | rising means money leaking — see [caching](caching.md) |
| `tokenbiryani_upstream_failures_total` | by class; `invalid_auth` needs a human |
| `tokenbiryani_failovers_total` | routine in small numbers |
