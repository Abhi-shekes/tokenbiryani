# tokenbiryani-oauth

Adds a `type: oauth` account to the [tokenbiryani](../../README.md) gateway, backed by
a Claude subscription session instead of an API key.

It is a **separate distribution on purpose**. The core gateway does not depend on it,
does not reference it, and works fine without it. This directory is self-contained —
its own `pyproject.toml`, LICENSE and tests — so it can be moved to its own repository
whenever you want, with no changes.

---

## Read this before installing

### It costs you most of the gateway

Every routing feature that makes this project different from a generic proxy is
computed from `anthropic-ratelimit-*` response headers. **Subscription sessions do not
send them.** There is nothing for the adapter to read, so:

| | API key account | `oauth` account |
|---|---|---|
| Failover across accounts | yes | yes |
| Prompt-cache affinity | yes | yes |
| Headroom-aware routing | yes | **no** |
| Leases that actually bind | yes | **no** — nothing to reserve against |
| Admission control (fail fast) | yes | **no** |
| Capacity horizon | yes | **no** |
| Behaviour when saturated | wait for a known reset | reactive 429 backoff |

Reactive backoff is what every generic proxy already does. You keep the two features
that carry most of the day-to-day value — failover, and the cache affinity worth
[1.94x](../../docs/caching.md) — and lose the rest.

### The terms question

Anthropic's consumer terms restrict sharing subscription accounts and using them
outside the provided interfaces. Pooling several subscription accounts so their limits
add up is what that is aimed at. The practical risks are yours: account suspension,
and for a public repository, a takedown request.

Routing **your own single** subscription through **your own local** gateway is a
different thing from pooling several to multiply limits. This package cannot tell the
difference, and does not try to. You are the one who knows which you are doing.

### A middle path worth considering first

The core gateway supports mixed pools. Put your subscription in at `cost_tier: 0`,
your API keys above it, and run `cost_tiered`: the router drains the subscription first
and spills to API keys when it is exhausted. Each credential gets used the way it is
licensed, and the paid capacity still gets real headroom-aware routing.

---

## Install

```bash
pip install tokenbiryani-oauth      # or: pip install -e contrib/tokenbiryani-oauth
```

Installing it is the whole integration. The `tokenbiryani.providers` entry point makes
`type: oauth` valid; nothing in core changes.

## Check your credentials first

This package has **never been run against a real subscription session**, so it cannot
promise the credentials file looks the way it expects. Rather than fail mysteriously,
it ships a command that shows you your own file:

```
$ tokenbiryani-oauth-check

  file        /home/you/.claude/.credentials.json
  keys        claudeAiOauth.accessToken: str
              claudeAiOauth.refreshToken: str
              claudeAiOauth.expiresAt: int
              claudeAiOauth.subscriptionType: str
  expiry      2026-09-06 11:04:12 (8.7h from now)
  token       sk-ant-oa…mnop  (at 'claudeAiOauth.accessToken')

  Looks usable. Configure the account as: …
```

Tokens are never printed whole. If your file has a different shape, the command prints
the keys it actually found and you point `token_path` at the right one.

## Configure

```yaml
accounts:
  - id: acct-oauth
    type: oauth
    # Required. Without it the account reads as permanently full and starves every
    # API-key account in the pool. The adapter warns loudly if you forget.
    observable_limits: false
    cost_tier: 0.0          # so `cost_tiered` drains it before paid capacity
    options:
      # All optional; these are the defaults.
      # credentials_path: ~/.claude/.credentials.json
      # token_path: claudeAiOauth.accessToken
      # expiry_path: claudeAiOauth.expiresAt
      # beta_header: oauth-2025-04-20
```

Alternatives to the credentials file, if you would rather manage tokens yourself:

```yaml
    options:
      token_env: MY_SESSION_TOKEN     # read from the environment
      # access_token: sk-ant-oat01-…  # pasted in; it will expire on you
```

## It does not refresh tokens, deliberately

There is no OAuth refresh flow here. Implementing one means hardcoding a token
endpoint and a client id that this package cannot verify, and getting either wrong
produces a confusing failure at the worst moment.

Instead the credentials file is re-read whenever its mtime changes. The CLI refreshes
that file on its own, so **using the CLI once renews the token and the gateway picks it
up without a restart.**

If the token does expire, the account is disabled with a message saying exactly that,
which shows up in `tokenbiryani status`, the console, and the request inspector. An
honest failure beats a silent one.

## What is unverified

Two things, both configurable so you can correct them:

- **The credentials file schema.** Defaults match the commonly reported shape. Run
  `tokenbiryani-oauth-check` to confirm against yours.
- **The `anthropic-beta` header value.** If requests come back 4xx complaining about a
  beta flag, set `options.beta_header`.

Everything else — header handling, token sourcing, expiry, the routing consequences —
is covered by the tests in `tests/`, which run against the core mock upstream.

## Moving it out of this repository

```bash
git subtree split --prefix=contrib/tokenbiryani-oauth -b oauth-adapter
```

Then push that branch to its own repo. Nothing in core references this directory.
