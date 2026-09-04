# Streaming and retries

## What the gateway can hide

A failure that happens **before** the first `content_block_delta` reaches the client is
retried silently on another account. The client sees one clean stream and never learns
that the first attempt died.

The gateway does this by holding the upstream stream and buffering until either the
first content delta arrives or `retry.first_token_grace_seconds` elapses. Until that
point nothing has been committed, so nothing has been lost.

## What it cannot

Once the first byte reaches the client, the client is already rendering. There is no
way to un-send it, so transparent failover is impossible. A failure after that point
arrives as an SSE `error` frame and the request ends.

!!! note "This boundary is real, not a limitation of this implementation"
    Any proxy faces it. What varies is whether the proxy tells you where the line is.

## The retry budget

Bounded three ways at once, whichever binds first:

- `retry.max_attempts` — total tries
- `retry.max_accounts` — distinct credentials tried
- `retry.deadline_seconds` — wall clock

Backoff is exponential with full jitter.

## What gets retried

Account action and request action are separate decisions. A `400` is the caller's
problem and is returned untouched; retrying it across the pool would burn quota on a
request that was never going to succeed.

| Upstream | Account | Request |
|---|---|---|
| `429` | cooldown until `retry-after` | retry elsewhere |
| `529` | short cooldown | retry |
| `500` / `502` / `503` | error tick, breaker may trip | retry with jitter |
| `401` / `403` auth | **disabled**, operator alerted | retry elsewhere |
| `403` model denied | model marked unsupported here | retry elsewhere |
| `400` / `413` | none — not the account's fault | **returned as-is, never retried** |

See [ADR-0002](adr/0002-error-taxonomy.md) for why these are two decisions and not one.

## Batches do not stream

A `batch`-priority request that would spill to the Message Batches API will not spill
if it is a streaming request. Batches are asynchronous and produce no stream.
