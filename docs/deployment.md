# Deployment

## The whole stack, from one file

```bash
docker compose up -d      # gateway + Redis + a mock Anthropic
docker compose down       # stop it
docker compose down -v    # stop it and forget the stored accounts too
```

Nothing needs setting first. The stack boots against the mock upstream bundled with
the package, so it comes up healthy, costs nothing and reaches nothing — then you open
`http://localhost:8787/console` and add real accounts there.

Three services, all in `docker-compose.yml`:

| | |
|---|---|
| `gateway` | the gateway, built from the Dockerfile's `dev` target |
| `mock` | the bundled fake Anthropic, so the pool has something to serve |
| `redis` | shared state: affinity, the spend ledger, usage history, stored accounts |

### Editing it while it runs

`./src` is bind-mounted and uvicorn watches it, so saving a Python file on the host
restarts the gateway in the container in about a second. The console's HTML and CSS
are read per request — a browser refresh is enough. `docker/tokenbiryani.yaml` is
mounted too, and the gateway re-reads it on mtime change, so config edits need no
restart either.

`./tests` is mounted as well, so the suite runs against the running stack:

```bash
docker compose exec gateway pytest -q
```

### Two keys, and they are not the same key

```bash
docker compose run --rm --no-deps gateway keygen           # TOKENBIRYANI_KEY
docker compose run --rm --no-deps gateway keygen --secret  # TOKENBIRYANI_SECRET_KEY
```

`TOKENBIRYANI_KEY` is the virtual key your clients authenticate with.
`TOKENBIRYANI_SECRET_KEY` encrypts account credentials before they reach Redis.

Redis outlives the container, so the second key has to as well: **lose it and every
account you added from the console is unrecoverable.** Compose falls back to a key on
a named volume when it is unset, which survives `down` but not `down -v` — fine for a
laptop, not for anything you care about. Copy `.env.example` to `.env` and put both
there; compose reads it automatically and `.env` is gitignored.

The defaults in `docker-compose.yml` are development values published in a public
repository. Replace them before this touches anything real.

## The released image

`docker compose` builds the Dockerfile's `dev` target. The default target is
`runtime`, which is what CI builds and what ships to GHCR: the wheel only, no source
tree, no test dependencies — about a third of the size.

```bash
docker build -t tokenbiryani .          # runtime, the default
docker build --target dev -t tb:dev .   # what compose uses
```

Both run as a non-root user and carry a healthcheck wired to `/healthz`, which reports
unhealthy exactly when no account is ready. Its port is read from
`TOKENBIRYANI_HEALTH_PORT`, so a config serving elsewhere is still checked correctly.

Binding `0.0.0.0` is the point of a container, so `docker/tokenbiryani.yaml` sets
`server.allow_remote: true` and defines a key. Without both, the gateway refuses to
start rather than expose your credentials to the network. Compose publishes to
`127.0.0.1` rather than `0.0.0.0`, so the container is not on your LAN by default.

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
- Set `TOKENBIRYANI_SECRET_KEY` rather than relying on the generated fallback, and
  back it up somewhere other than the store it protects.
- Replace the development keys in `docker-compose.yml`. They are in the repository.

## Metrics

Scrape `/metrics`. The series worth alerting on:

| | |
|---|---|
| `tokenbiryani_account_headroom` | per account, 0 means no budget left |
| `tokenbiryani_queue_depth` | sustained non-zero means the pool is undersized |
| `tokenbiryani_cache_breaks_total` | rising means money leaking — see [caching](caching.md) |
| `tokenbiryani_upstream_failures_total` | by class; `invalid_auth` needs a human |
| `tokenbiryani_failovers_total` | routine in small numbers |
