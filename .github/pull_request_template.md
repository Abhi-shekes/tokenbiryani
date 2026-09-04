## What this changes

<!-- One or two sentences. -->

## Why

<!-- The problem, not the patch. -->

## Checks

- [ ] `ruff check .`
- [ ] `mypy`
- [ ] `pytest`
- [ ] Tests cover the behaviour, not the implementation detail

## House rules this touches

<!-- Delete what does not apply, and say how you kept to the rest. -->

- [ ] **Router, not a rewriter** — nothing modifies a request body outside
      `providers/translate.py`
- [ ] **Shared state goes through `StateStore`** — no reaching around it
- [ ] **New failure modes go in the taxonomy** — account action and request action
      stay separate decisions
- [ ] **No prompt bodies logged** on any default path
- [ ] Cache hit rate is not quietly traded away — `benchmarks/cache_affinity.py`
      still shows sticky winning
