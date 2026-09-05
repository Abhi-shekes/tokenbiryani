"""The fake Anthropic API the whole suite runs against.

It lives here, and not in the installed package, because it is test scaffolding:
shipping it meant every `pip install tokenbiryani` also installed a runnable fake
Anthropic server under `tokenbiryani.testing`, which is not something a user of a
gateway has any reason to be given.

`tests/` is already on `sys.path` for the suite (pytest puts it there for
`conftest.py`), so tests import it as `support.mock_upstream`. Anything outside the
suite that needs it — `scripts/smoke.sh`, `scripts/dev.sh`, `benchmarks/` — puts
`tests/` on `PYTHONPATH` and runs `python -m support.server`.
"""
