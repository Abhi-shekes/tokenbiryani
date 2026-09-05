# Quickstart

## Install and run

```bash
pip install tokenbiryani     # or: pipx install tokenbiryani
tokenbiryani init            # writes tokenbiryani.yaml and a virtual key
tokenbiryani serve
```

Nothing else needs to be exported. `init` writes an admin key, the SQLite store — so
spend caps and affinity survive a restart — and an **empty pool**, because the console
is where the first account goes.

## Add your first account

```bash
tokenbiryani console         # opens the browser, already signed in
```

That is the whole sign-in step: the command mints a single-use ticket, opens the
console on it, and the page exchanges it for the admin key. The key itself never
travels in the URL.

The wizard then does three things — takes a credential, **verifies it** (the same
rate-limit-header check `tokenbiryani doctor` performs, without the spend), and hands
you the two lines that use it, with a real key already in them.

Prefer the terminal, or scripting it?

```bash
tokenbiryani accounts add work --api-key sk-ant-...   # probes it before storing
tokenbiryani accounts test                            # every account, with the header check
tokenbiryani accounts list
```

## Point your tools at it

```bash
export ANTHROPIC_BASE_URL=http://localhost:8787
export ANTHROPIC_AUTH_TOKEN=bir_...    # copy it from the console's Connect screen
claude
```

No client changes. The gateway speaks the Messages API verbatim.

## Add a second account

```yaml
accounts:
  - id: acct-01
    type: anthropic_api
    api_key: ${ANTHROPIC_API_KEY}
  - id: acct-02
    type: anthropic_api
    api_key: ${ANTHROPIC_API_KEY_2}
```

The file is re-read when it changes, so there is no restart. Accounts that survive a
reload keep their health: a cooling account is not handed a clean slate.

An account declared in the file belongs to the file — the console will not edit it.
Accounts added from the console or the CLI live in the store, encrypted, and can be
renamed, tested and rotated in place.

## Make the cost column mean something

```yaml
pricing: builtin
```

That takes the dated price table shipped with this release; the Settings screen shows
the date beside it, so a stale table is visible rather than silent. Override any line
by naming the model:

```yaml
pricing:
  builtin: true
  claude-opus-5: {input: 5.0, output: 25.0, cache_read: 0.5, cache_write: 6.25}
```

Without it, costs read `—`: spend is only reported, and caps only enforced, for models
that have a price.

## Watch it work

```bash
tokenbiryani status          # the pool, in the terminal
open http://localhost:8787/console
```

The console asks for an admin key on first load and keeps it in that browser only.

## Verify without spending anything

`tokenbiryani accounts test` probes every credential without sending a completion, and
`tokenbiryani doctor` sends exactly one (`max_tokens=1`) to check that the upstream
spells its rate-limit headers the way the router expects.

A scriptable fake Anthropic API lives in the repository — `tests/support/` — for
reproducing failure modes offline: a 429 with a `retry-after`, a 529, a stream that
dies mid-flight. It is test scaffolding rather than part of the installed package, so
it comes from a clone, not from `pip install` — see `CONTRIBUTING.md`.
