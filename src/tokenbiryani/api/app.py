"""HTTP surface. Anthropic-compatible on /v1, gateway-specific on /admin."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from ..config import Config, KeyConfig
from ..core.gateway import Gateway, GatewayError

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

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await app.state.gateway.aclose()

    def authenticate(request: Request) -> KeyConfig:
        result = app.state.gateway.keys.authenticate(_credential(request))
        if not result.ok or result.key is None:
            raise GatewayError(401, result.error, kind="authentication_error")
        return result.key

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
        authenticate(request)
        return JSONResponse(app.state.gateway.snapshot())

    @app.get("/admin/horizon")
    async def horizon(request: Request) -> JSONResponse:
        authenticate(request)
        return JSONResponse(app.state.gateway.capacity_horizon())

    @app.get("/admin/keys")
    async def keys(request: Request) -> JSONResponse:
        authenticate(request)
        return JSONResponse({"keys": app.state.gateway.keys.redacted()})

    @app.get("/admin/requests")
    async def requests(request: Request, limit: int = 50) -> JSONResponse:
        authenticate(request)
        return JSONResponse({"requests": app.state.gateway.events.recent(limit)})

    @app.get("/admin/requests/{request_id}")
    async def request_detail(request: Request, request_id: str) -> JSONResponse:
        authenticate(request)
        found = app.state.gateway.events.get(request_id)
        if found is None:
            return _error(404, f"no such request: {request_id}", "not_found_error")
        return JSONResponse(found)

    @app.get("/admin/events")
    async def events(request: Request) -> StreamingResponse:
        authenticate(request)
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
