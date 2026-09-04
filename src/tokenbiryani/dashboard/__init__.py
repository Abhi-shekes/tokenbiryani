"""The operator console: a server-rendered page and its stylesheet, no build step.

It ships inside the package deliberately. Adding a Node toolchain to a `pipx install`
to get a dashboard would be a bad trade for a tool whose whole appeal is that it runs
from one command.

The CSS is a separate file rather than a `<style>` block because it is a design system
— tokens, primitives, components — that is read and edited on its own terms. Splitting
it costs one cached request and no build step at all.
"""

from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE_PATH = os.path.join(_HERE, "console.html")
CONSOLE_CSS_PATH = os.path.join(_HERE, "console.css")

_cache: dict = {}


def _read(path: str) -> str:
    """Read once and keep it; neither file changes at runtime."""
    if path not in _cache:
        with open(path, encoding="utf-8") as handle:
            _cache[path] = handle.read()
    return _cache[path]


def console_html() -> str:
    return _read(CONSOLE_PATH)


def console_css() -> str:
    return _read(CONSOLE_CSS_PATH)
