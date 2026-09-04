"""The operator console: a server-rendered page and its stylesheet, no build step.

It ships inside the package deliberately. Adding a Node toolchain to a `pipx install`
to get a dashboard would be a bad trade for a tool whose whole appeal is that it runs
from one command.

The CSS is a separate file rather than a `<style>` block because it is a design system
— tokens, primitives, components — that is read and edited on its own terms. Splitting
it costs one cached request and no build step at all.

Both are re-read when they change on disk, so editing the console and refreshing the
browser is the whole loop — no restart, and no build.
"""

from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE_PATH = os.path.join(_HERE, "console.html")
CONSOLE_CSS_PATH = os.path.join(_HERE, "console.css")

#: path -> (mtime, contents)
_cache: dict = {}


def _read(path: str) -> str:
    """Read once, and again whenever the file changes underneath us.

    Caching outright was wrong. In a container with the source bind-mounted, and
    under `serve --reload` (which watches Python files, not these), editing the
    console meant refreshing the browser and seeing the old page until something
    else happened to restart the process.

    A stat per request is nothing next to proxying a model call, and it makes
    "edit the console, refresh" true rather than nearly true.
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    with open(path, encoding="utf-8") as handle:
        contents = handle.read()
    _cache[path] = (mtime, contents)
    return contents


def console_html() -> str:
    return _read(CONSOLE_PATH)


def console_css() -> str:
    return _read(CONSOLE_CSS_PATH)
