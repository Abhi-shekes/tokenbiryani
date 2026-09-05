# Configuration

One file, `tokenbiryani.yaml`. `${VAR}` and `${VAR:-default}` are expanded from the
environment; a reference to an unset variable with no default is an error at load
rather than a mystery at runtime.

The complete annotated reference is
`tokenbiryani.example.yaml`, in the repository root.
This page covers what tends to need explaining.

## Hot reload

The file is re-read when its mtime changes (`server.hot_reload_seconds`, 0 disables),
and `POST /admin/reload` forces it.

Accounts that survive a reload **keep their runtime state** — limit mirror, breaker,
counters. Editing an unrelated setting must not hand a cooling account a clean bill of
health and re-stampede an upstream. The exception is rotated credentials, which do
clear a disabled account, because a rotated key is usually why it was disabled.

A file that fails to parse is rejected and the running config is kept.

## Prices

Costs are reported, and spend caps enforced, only for models that have a price. Two
ways to give them one.

**The table that ships with this release**, dated:

```yaml
pricing: builtin
```

The Settings screen shows its `as_of` date beside every cost, so a stale table is
visible rather than silent. Check the current numbers at
[anthropic.com/pricing](https://anthropic.com/pricing).

**Your own**, which always wins:

```yaml
pricing:
  builtin: true          # optional — start from the shipped table
  claude-opus-4-*:
    input: 15.0
    output: 75.0
    cache_read: 1.5
    cache_write: 18.75
```

USD per million tokens. Prices live in a dated data file rather than in code for the
reason they always did: the gateway must never bill you against a number nobody can
attribute. A dated file the console shows the date of can be attributed. A dict
compiled into a release cannot.

Without either, every cost reads `—`, and the Usage screen says so rather than
drawing an empty chart.

## Spend caps are windowed

`spend.window_hours` (default 24) is a rolling window, not a lifetime total. A lifetime
cap on a persistent store would eventually wedge the gateway shut and stay that way,
which is not what an operator means by "cap".

Caps only survive a restart if the store does — `store.backend: memory` loses the
ledger, silently resetting every cap.

## Per-conversation caps

A key cap bounds everything a key does. It cannot see a runaway *inside* that
allowance — one agent loop resending a large prefix a few hundred times is the whole
cap with that key's name on it, and there is nothing to look at until it trips.

```yaml
sessions:
  track: true              # record per-session cost and turns
  runaway_turns: 200       # what GET /admin/sessions flags
  runaway_spend_usd: null
```

`sessions.track` writes two extra ledger rows per request; those rows are what the
caps below and the report are made of. Turning it off makes both go quiet — and stops
the caps being enforced, since the counters they read are the ones tracking writes.

## Sizing the output lease

```yaml
routing:
  output_estimate: adaptive     # or max_tokens
  output_estimate_quantile: 0.95
  output_estimate_min_samples: 20
```

`adaptive` leases what a model has actually been returning rather than the caller's
`max_tokens`, bounded by that ceiling either way — it can only reserve *less*. See
[Routing](routing.md#sizing-the-output-half) for why the ceiling starves a pool.

## Prompt-cache diagnosis

```yaml
cache:
  auto_breakpoint: false   # leave it off unless you need it
  advice_min_requests: 20
```

Diagnosis (`GET /admin/cache-advice`) is always on and costs nothing.
`auto_breakpoint` makes the gateway *edit* the caller's request, which it otherwise
refuses to do — see [Why is my bill higher?](caching.md).

## Quota pacing

```yaml
pacing:
  enabled: true
  mode: advisory           # advisory | enforcing
  curve: linear            # or business_hours
  weekly_budget_usd: null  # required for API-key accounts
```

Subscriptions report their own rolling windows and are paced automatically. API keys
report no weekly window at all, so without a stated budget they are simply not paced.
Full detail in [Spending the week on purpose](pacing.md).

## Keys

```yaml
keys:
  - key: ${TOKENBIRYANI_KEY}
    name: default
    admin: true          # required for /admin/*
    models: ["*"]
    pool: []             # empty means every account
    rpm: null
    spend_cap_usd: null
    session_cap_usd: null    # bounds one conversation
    session_max_turns: null  # within this key's allowance
    priority: interactive
    max_wait_seconds: null
```

`admin` gates the whole `/admin` surface, which exposes account ids, spend and key
management. Issue tenant keys without it. A gateway with **no** keys at all is
loopback development mode and stays open; it refuses to bind a public interface in
that state.

Keys minted at runtime live in the store instead — see [Operating](operating.md).

## Binding

```yaml
server:
  host: 127.0.0.1
  allow_remote: false
```

The gateway refuses to bind anything but loopback without `allow_remote: true` **and**
at least one configured key. Both, not either.
