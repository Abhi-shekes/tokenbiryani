"""The operator console: one server-rendered HTML file, no build step.

It ships inside the package deliberately. Adding a Node toolchain to a `pipx install`
to get a dashboard would be a bad trade for a tool whose whole appeal is that it runs
from one command.
"""

from __future__ import annotations

import os

CONSOLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console.html")

_cached = None


def console_html() -> str:
    """Read the console once and keep it; it never changes at runtime."""
    global _cached
    if _cached is None:
        with open(CONSOLE_PATH, encoding="utf-8") as handle:
            _cached = handle.read()
    return _cached
