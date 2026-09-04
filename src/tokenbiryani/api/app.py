"""HTTP surface. Anthropic-compatible on /v1, gateway-specific on /admin."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from ..config import Config, KeyConfig
from ..core.gateway import Gateway, GatewayError
from ..dashboard import console_html

ANTHROPIC_PREFIX = "/v1"


def _credential(request: Request) -> Optional[str]:
    presented = request.headers.get("x-api-key")
    if presented:
        return presented
    authorization = request.headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _error(status: int, message: str, kind: str = "api_error", headers=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": kind, "message": message}},
        headers=headers or {},
    )


def create_app(config: Config, gateway: Optional[Gateway] = None) -> FastAPI:
    app = FastAPI(title="tokenbiryani", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.gateway = gateway or Gateway(config)
    app.state.config = config

    @app.on_event("startup")
    async def _startup() -> None:
        await app.state.gateway.startup()
        app.state.watcher = asyncio.ensure_future(app.state.gateway.watch_config())
        app.state.resync = asyncio.ensure_future(app.state.gateway.resync_spend())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        for name in ("watcher", "resync"):
            task = getattr(app.state, name, None)
            if task is not None:
                task.cancel()
        await app.state.gateway.aclose()

    def authenticate(request: Request) -> KeyConfig:
        result = app.state.gateway.keys.authenticate(_credential(request))
        if not result.ok or result.key is None:
            raise GatewayError(401, result.error, kind="authentication_error")
        return result.key

    def authenticate_admin(request: Request) -> KeyConfig:
        """/admin exposes account ids, spend and key management. Tenants stay out."""
        key = authenticate(request)
        if not key.admin:
            raise GatewayError(
                403,
                f"key {key.name!r} is not an admin key",
                kind="permission_error",
            )
        return key

    async def read_body(request: Request) -> Dict[str, Any]:
        raw = await request.body()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise GatewayError(
                400, f"request body is not valid JSON: {exc}", kind="invalid_request_error"
            ) from exc
        if not isinstance(parsed, dict):
            raise GatewayError(400, "request body must be an object", kind="invalid_request_error")
        return parsed

    @app.exception_handler(GatewayError)
    async def _gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.payload(), headers=exc.headers())

    # ---- Anthropic-compatible surface ---------------------------------------

    @app.post(ANTHROPIC_PREFIX + "/messages")
    async def messages(request: Request) -> Response:
        key = authenticate(request)
        body = await read_body(request)
        gateway: Gateway = app.state.gateway

        if body.get("stream"):
            headers, iterator = await gateway.stream(body, request.headers, key)
            return StreamingResponse(iterator, media_type="text/event-stream", headers=headers)

        completion = await gateway.complete(body, request.headers, key)
        return Response(
            content=completion.content,
            status_code=completion.status,
            headers=completion.headers,
            media_type="application/json",
        )

    @app.post(ANTHROPIC_PREFIX + "/messages/count_tokens")
    async def count_tokens(request: Request) -> Response:
        key = authenticate(request)
        body = await read_body(request)
        completion = await app.state.gateway.simple_request(
            "POST", "/v1/messages/count_tokens", key, request.headers, body
        )
        return Response(
            content=completion.content,
            status_code=completion.status,
            headers=completion.headers,
            media_type="application/json",
        )

    @app.get(ANTHROPIC_PREFIX + "/models")
    async def models(request: Request) -> Response:
        key = authenticate(request)
        completion = await app.state.gateway.simple_request(
            "GET", "/v1/models", key, request.headers
        )
        return Response(
            content=completion.content,
            status_code=completion.status,
            headers=completion.headers,
            media_type="application/json",
        )

    # ---- operational surface -------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def root() -> Response:
        return RedirectResponse("/console")

    @app.get("/console", include_in_schema=False)
    async def console() -> Response:
        # The shell carries no data, so it needs no key. Every call it makes is
        # authenticated, and the key it uses never leaves the browser.
        return HTMLResponse(console_html())

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        snapshot = app.state.gateway.snapshot()
        healthy = snapshot["pool"]["ready"] > 0
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ok" if healthy else "no_capacity",
                "ready": snapshot["pool"]["ready"],
                "total": snapshot["pool"]["total"],
            },
        )

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            app.state.gateway.metrics.render(), media_type="text/plain; version=0.0.4"
        )

    @app.get("/admin/status")
    async def status(request: Request) -> JSONResponse:
        authenticate_admin(request)
        return JSONResponse(app.state.gateway.snapshot())

    @app.get("/admin/horizon")
    async def horizon(request: Request) -> JSONResponse:
        authenticate_admin(request)
        return JSONResponse(app.state.gateway.capacity_horizon())

    @app.get("/admin/accounts/{account_id}")
    async def account_detail(request: Request, account_id: str) -> JSONResponse:
        authenticate_admin(request)
        gateway: Gateway = app.state.gateway
        account = gateway.accounts.get(account_id)
        if account is None:
            return _error(404, f"no such account: {account_id}", "not_found_error")
        payload = account.snapshot(time.time())
        payload["recent_requests"] = gateway.events.recent_for(account_id, 25)
        return JSONResponse(payload)

    @app.get("/admin/accounts")
    async def list_accounts(request: Request) -> JSONResponse:
        authenticate_admin(request)
        gateway: Gateway = app.state.gateway
        await gateway.refresh_accounts()
        return JSONResponse(gateway.snapshot())

    @app.post("/admin/accounts")
    async def create_account(request: Request) -> JSONResponse:
        authenticate_admin(request)
        payload = await read_body(request)
        record = await app.state.gateway.create_account(
            str(payload.get("id") or ""),
            str(payload.get("name") or ""),
            str(payload.get("api_key") or ""),
            type=payload.get("type"),
            base_url=payload.get("base_url"),
            cost_tier=payload.get("cost_tier"),
            priority=payload.get("priority"),
            models=payload.get("models"),
            spend_cap_usd=payload.get("spend_cap_usd"),
            observable_limits=payload.get("observable_limits", True),
            options=payload.get("options"),
        )
        return JSONResponse(record, status_code=201)

    @app.patch("/admin/accounts/{account_id}")
    async def update_account(request: Request, account_id: str) -> JSONResponse:
        authenticate_admin(request)
        payload = await read_body(request)
        return JSONResponse(await app.state.gateway.update_account(account_id, **payload))

    @app.delete("/admin/accounts/{account_id}")
    async def delete_account(request: Request, account_id: str) -> JSONResponse:
        authenticate_admin(request)
        removed = await app.state.gateway.delete_account(account_id)
        if not removed:
            return _error(404, f"no managed account named {account_id!r}", "not_found_error")
        return JSONResponse({"deleted": account_id})

    @app.post("/admin/accounts/{account_id}/test")
    async def test_account(request: Request, account_id: str) -> JSONResponse:
        authenticate_admin(request)
        return JSONResponse(await app.state.gateway.test_account(account_id))

    @app.post("/admin/reload")
    async def reload(request: Request) -> JSONResponse:
        authenticate_admin(request)
        return JSONResponse(app.state.gateway.reload_from_path())

    @app.get("/admin/keys")
    async def keys(request: Request) -> JSONResponse:
        authenticate_admin(request)
        return JSONResponse({"keys": app.state.gateway.keys.redacted()})

    @app.post("/admin/keys")
    async def create_key(request: Request) -> JSONResponse:
        authenticate_admin(request)
        payload = await read_body(request)
        name = str(payload.get("name") or "")
        plaintext, record = await app.state.gateway.create_key(
            name,
            models=payload.get("models"),
            pool=payload.get("pool"),
            rpm=payload.get("rpm"),
            spend_cap_usd=payload.get("spend_cap_usd"),
            priority=payload.get("priority"),
            max_wait_seconds=payload.get("max_wait_seconds"),
            admin=payload.get("admin"),
        )
        # The only time the plaintext exists outside the caller's hands.
        return JSONResponse({"key": plaintext, "record": record}, status_code=201)

    @app.delete("/admin/keys/{name}")
    async def revoke_key(request: Request, name: str) -> JSONResponse:
        authenticate_admin(request)
        removed = await app.state.gateway.revoke_key(name)
        if not removed:
            return _error(404, f"no managed key named {name!r}", "not_found_error")
        return JSONResponse({"revoked": name})

    @app.get("/admin/requests")
    async def requests(request: Request, limit: int = 50) -> JSONResponse:
        authenticate_admin(request)
        return JSONResponse({"requests": app.state.gateway.events.recent(limit)})

    @app.get("/admin/requests/{request_id}")
    async def request_detail(request: Request, request_id: str) -> JSONResponse:
        authenticate_admin(request)
        found = app.state.gateway.events.get(request_id)
        if found is None:
            return _error(404, f"no such request: {request_id}", "not_found_error")
        return JSONResponse(found)

    @app.get("/admin/events")
    async def events(request: Request) -> StreamingResponse:
        authenticate_admin(request)
        log = app.state.gateway.events

        async def feed():
            queue = log.subscribe()
            try:
                yield b": connected\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield ("data: " + json.dumps(payload) + "\n\n").encode("utf-8")
            finally:
                log.unsubscribe(queue)

        return StreamingResponse(feed(), media_type="text/event-stream")

    return app
