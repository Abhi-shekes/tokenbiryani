"""A stable ASGI entry point: ``tokenbiryani.api.asgi:create``.

`tokenbiryani serve` builds the app from a `Config` object it already holds, which
is fine until you want reloading — uvicorn's reloader re-imports the app in a fresh
worker process, so it needs an import string rather than an object.

This is that import string, and it is useful beyond reloading: it is also how you
put the gateway behind gunicorn, or any other ASGI runner, without going through
the CLI at all.

    TOKENBIRYANI_CONFIG=/etc/tokenbiryani.yaml \\
      uvicorn --factory tokenbiryani.api.asgi:create
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from ..config import Config

#: Which config file to read. The CLI sets this before handing over to the reloader.
CONFIG_ENV = "TOKENBIRYANI_CONFIG"
DEFAULT_CONFIG = "tokenbiryani.yaml"


def create() -> FastAPI:
    """Build the app from the config named by $TOKENBIRYANI_CONFIG."""
    from .app import create_app

    return create_app(Config.load(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG))
