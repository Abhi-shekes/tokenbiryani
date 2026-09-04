# Quickstart

## Install and run

```bash
pip install tokenbiryani     # or: pipx install tokenbiryani
tokenbiryani init            # writes tokenbiryani.yaml and prints a virtual key
export ANTHROPIC_API_KEY=sk-ant-...
tokenbiryani serve
```

`init` writes a config with one account, one admin key, and the SQLite store, so spend
caps and affinity survive a restart.

## Point your tools at it

```bash
export ANTHROPIC_BASE_URL=http://localhost:8787
export ANTHROPIC_AUTH_TOKEN=bir_...    # printed by `init`
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

## Watch it work

```bash
tokenbiryani status          # the pool, in the terminal
open http://localhost:8787/console
```

The console asks for an admin key on first load and keeps it in that browser only.

## Verify without spending anything

A scriptable fake Anthropic API ships in the package:

```bash
python -m tokenbiryani.testing.server --port 9911 --accounts key-a,key-b
```

Point an account's `base_url` at it and every failure mode — 429 with a `retry-after`,
529, a stream that dies mid-flight — is reproducible offline.
