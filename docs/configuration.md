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

The gateway ships **no price list**. Costs are reported, and spend caps enforced, only
for models named under `pricing:`:

```yaml
pricing:
  claude-opus-4-*:
    input: 15.0
    output: 75.0
    cache_read: 1.5
    cache_write: 18.75
```

USD per million tokens. Baking prices into code would mean silently billing you
against numbers that went stale in a release.

## Spend caps are windowed

`spend.window_hours` (default 24) is a rolling window, not a lifetime total. A lifetime
cap on a persistent store would eventually wedge the gateway shut and stay that way,
which is not what an operator means by "cap".

Caps only survive a restart if the store does — `store.backend: memory` loses the
ledger, silently resetting every cap.

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
