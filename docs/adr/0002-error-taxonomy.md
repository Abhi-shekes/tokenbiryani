# 2. Account action and request action are separate decisions

**Status:** accepted

## Context

The tempting shape for retry logic is "if the status is retryable, try the next
account." That produces the classic failure-amplification bug: a client sends a
malformed request, gets a `400`, and the gateway replays it against every credential in
the pool — burning quota on a request that was never going to succeed.

## Decision

Every upstream outcome maps to exactly two independent decisions in `proxy/errors.py`:

- **Account action** — none, error tick, cooldown, short cooldown, disable, or mark the
  model unsupported.
- **Request action** — retry elsewhere, retry anywhere, or return to the caller.

They are genuinely independent. A `403` naming a model marks that model unsupported on
that account and retries elsewhere. A `400` touches the account not at all and is
returned untouched.

## Consequences

- New failure modes are added to one table, not scattered across call sites.
- The taxonomy is directly testable without a gateway (`tests/test_errors.py`).
- Some upstream responses are deliberately not retried even though they look transient.
