# Operating

## The console

`http://localhost:8787/console` — pool, capacity horizon, live request feed, routing
inspector, per-account detail, key management.

One server-rendered HTML file inside the package: no build step, and no Node toolchain
added to a `pipx install`. The shell carries no data and needs no key; it asks for an
admin key on first load and keeps it in that browser only.

## The CLI

```bash
tokenbiryani status          # the pool
tokenbiryani status --json   # same data, for scripts
tokenbiryani strategies      # what this install can route with
tokenbiryani keygen          # a new virtual key
```

`status` reads `TOKENBIRYANI_URL` and `ANTHROPIC_AUTH_TOKEN`, and respects `NO_COLOR`.

## Endpoints

| | |
|---|---|
| `POST /v1/messages` | Messages API, streaming and not |
| `POST /v1/messages/count_tokens`, `GET /v1/models` | passthrough |
| `GET /healthz` | 200 while any account is ready |
| `GET /metrics` | Prometheus |
| `GET /console` | the operator console |
| `GET /admin/status` | pool snapshot |
| `GET /admin/accounts/{id}` | one account, with errors by class |
| `GET /admin/requests/{id}` | **why that request went where it did** |
| `GET /admin/horizon` | projected capacity for the next hour |
| `GET /admin/events` | live SSE feed |
| `POST /admin/keys`, `DELETE /admin/keys/{name}` | mint and revoke |
| `POST /admin/reload` | re-read the config file |

## Managing keys at runtime

```bash
curl -sX POST localhost:8787/admin/keys -H "x-api-key: $ADMIN_KEY" \
  -d '{"name":"tenant-1","pool":["acct-02"],"rpm":60,"spend_cap_usd":5}'
```

The plaintext appears **once**, in that response. Minted keys are stored hashed, so a
leaked state store is not a leaked key, and they live in the shared store — one
instance honours a key another minted.

Keys declared in the config file belong to the file; the API will not revoke them.

## Request headers

| Header | |
|---|---|
| `X-TokenBiryani-Session` | pin a conversation to one affinity key |
| `X-TokenBiryani-Priority` | `interactive` (default) or `batch` |
| `X-TokenBiryani-Max-Wait` | seconds to wait for capacity |

A client can shorten its own wait budget but never extend it past the operator's
ceiling.

## Privacy

Prompt bodies are **never logged** unless `observability.log_bodies` is explicitly
enabled. Logs carry ids, accounts, tokens, latency and cost — the accounting, not the
content.
