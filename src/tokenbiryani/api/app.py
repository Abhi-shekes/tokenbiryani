"""HTTP surface. Anthropic-compatible on /v1, gateway-specific on /admin."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from ..config import Config, KeyConfig
from ..core.gateway import Gateway, GatewayError
from ..core.handoff import HandoffError, TicketBook, is_loopback
from ..dashboard import console_css, console_html
from ..providers.oauth_credentials import detect_credentials

ANTHROPIC_PREFIX = "/v1"


def _escape(value: str) -> str:
    """The OAuth callback echoes provider-supplied text into HTML."""
    return (
        str(value)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


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
    app.state.tickets = TicketBook()

    @app.on_event("startup")
    async def _startup() -> None:
        await app.state.gateway.startup()
        app.state.watcher = asyncio.ensure_future(app.state.gateway.watch_config())
        app.state.resync = asyncio.ensure_future(app.state.gateway.resync_spend())
        app.state.sessions = asyncio.ensure_future(app.state.gateway.watch_oauth_sessions())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        for name in ("watcher", "resync", "sessions"):
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

    @app.get("/console.css", include_in_schema=False)
    async def console_stylesheet() -> Response:
        # no-cache, not no-store: the browser may keep it, but must revalidate.
        # The shell is never cached, so a stylesheet the browser held across an
        # upgrade would style the new markup with the old rules.
        return Response(
            console_css(),
            media_type="text/css",
            headers={"cache-control": "no-cache"},
        )

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

    @app.get("/admin/status")
    async def status(request: Request) -> JSONResponse:
        authenticate_admin(request)
        # Merge in anything managed that was added elsewhere — another replica, the
        # API, a second console tab. This is the console's polling endpoint, so
        # without it an account you just created stays invisible until a restart.
        await app.state.gateway.refresh_accounts()
        return JSONResponse(app.state.gateway.snapshot())

    @app.get("/admin/horizon")
    async def horizon(request: Request) -> JSONResponse:
        authenticate_admin(request)
        return JSONResponse(app.state.gateway.capacity_horizon())

    @app.get("/admin/estimation")
    async def estimation(request: Request) -> JSONResponse:
        """What the output estimator has learned, per model.

        Worth looking at before trusting it: `predicting: false` on a model means
        every request for it is still leasing the caller's full `max_tokens`.
        """
        authenticate_admin(request)
        return JSONResponse(app.state.gateway.estimator.snapshot())

    @app.get("/admin/usage")
    async def usage(
        request: Request,
        window: Optional[str] = None,
        bucket: Optional[str] = None,
        group_by: str = "account",
        account: Optional[str] = None,
    ) -> JSONResponse:
        """Persisted usage, bucketed for charting.

        `window` accepts a label the console sends (`1h`, `24h`, `7d`, `30d`) or a
        raw second count; `group_by` is account, model or key.
        """
        authenticate_admin(request)
        return JSONResponse(
            await app.state.gateway.usage(
                window=window, bucket=bucket, group_by=group_by, account_id=account
            )
        )

    @app.get("/admin/accounts/{account_id}")
    async def account_detail(request: Request, account_id: str) -> JSONResponse:
        authenticate_admin(request)
        payload = app.state.gateway.account_detail(account_id)
        if payload is None:
            return _error(404, f"no such account: {account_id}", "not_found_error")
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

    @app.delete("/admin/accounts/{account_id}/override")
    async def clear_account_override(request: Request, account_id: str) -> JSONResponse:
        """Drop console edits to a config account, restoring what the file says."""
        authenticate_admin(request)
        gateway: Gateway = app.state.gateway
        if not gateway.is_config_account(account_id):
            return _error(
                404,
                f"{account_id!r} is not declared in the config file, so it has no "
                "override to clear",
                "not_found_error",
            )
        cleared = await gateway.clear_override(account_id)
        return JSONResponse({"cleared": cleared, "account": gateway.account_detail(account_id)})

    @app.post("/admin/accounts/test")
    async def test_credential(request: Request) -> JSONResponse:
        """Probe a credential that has not been stored.

        The console calls this before POSTing the account, so a typo'd key is a
        message in a dialog rather than a disabled row to clean up afterwards.
        """
        authenticate_admin(request)
        payload = await read_body(request)
        result = await app.state.gateway.probe_credential(
            str(payload.get("api_key") or ""),
            type=payload.get("type"),
            base_url=payload.get("base_url"),
            options=payload.get("options"),
        )
        result.pop("headers", None)
        result.pop("classification", None)
        return JSONResponse(result)

    @app.post("/admin/accounts/{account_id}/test")
    async def test_account(request: Request, account_id: str) -> JSONResponse:
        authenticate_admin(request)
        return JSONResponse(await app.state.gateway.test_account(account_id))

    @app.post("/admin/accounts/{account_id}/diagnose")
    async def diagnose_account(request: Request, account_id: str) -> JSONResponse:
        """What `tokenbiryani doctor` reports, for one account, without the spend.

        The check it performs is the one open risk in the project: if the upstream
        spells a rate-limit header differently, routing degrades to round-robin and
        nothing looks wrong. Putting it behind the console's verify step is what
        makes it run for people who never learn the command exists.
        """
        authenticate_admin(request)
        payload = await read_body(request)
        # `spend: true` is the caller accepting one `max_tokens=1` completion, which
        # is the only way to see the limit headers on an account that has served no
        # traffic yet. Never the default: nothing here spends money unasked.
        return JSONResponse(await app.state.gateway.diagnose_account(
            account_id, spend=bool(payload.get("spend"))
        ))

    # ---- subscription login --------------------------------------------------

    @app.get("/admin/oauth/config")
    async def oauth_config(request: Request) -> JSONResponse:
        """Whether "Log in with Claude" is usable, so the console can say why not."""
        authenticate_admin(request)
        gateway: Gateway = app.state.gateway
        settings = config.oauth
        return JSONResponse({
            "configured": gateway.oauth.configured,
            "manual": not settings.redirect_uri,
            "redirect_uri": settings.redirect_uri,
            "missing": [
                name for name, value in (
                    ("oauth.client_id", settings.client_id),
                    ("oauth.authorize_url", settings.authorize_url),
                    ("oauth.token_url", settings.token_url),
                ) if not value
            ],
        })

    @app.get("/admin/oauth/detect")
    async def oauth_detect(request: Request) -> JSONResponse:
        """Claude Code logins on this machine, so the console can offer one.

        Never returns a token — only which file, which subscription, and whether it
        is current. Picking the wrong file is the failure this exists to prevent:
        with CLAUDE_CONFIG_DIR set, ~/.claude holds a different account entirely and
        the resulting 401 says nothing about which of the two was read.
        """
        authenticate_admin(request)
        return JSONResponse({"candidates": detect_credentials()})

    @app.post("/admin/oauth/start")
    async def oauth_start(request: Request) -> JSONResponse:
        authenticate_admin(request)
        payload = await read_body(request)
        return JSONResponse(await app.state.gateway.oauth_start(
            str(payload.get("id") or ""), str(payload.get("name") or "")
        ))

    @app.post("/admin/oauth/complete")
    async def oauth_complete(request: Request) -> JSONResponse:
        authenticate_admin(request)
        payload = await read_body(request)
        record = await app.state.gateway.oauth_complete(
            str(payload.get("state") or ""), str(payload.get("code") or "")
        )
        return JSONResponse(record, status_code=201)

    @app.get("/admin/oauth/callback", include_in_schema=False)
    async def oauth_callback(code: str = "", state: str = "", error: str = "") -> Response:
        """Where the provider lands when a redirect_uri is configured.

        Deliberately keyless and deliberately does not complete the exchange: the
        provider redirects a browser here, and that browser carries no admin key.
        It hands the code back to the console, which completes the login with one.
        """
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8>"
            "<title>tokenbiryani — authorized</title>"
            "<body style='font:14px system-ui;margin:64px auto;max-width:420px'>"
            + (
                f"<h3>Authorization failed</h3><p>{_escape(error)}</p>"
                if error else
                "<h3>Authorized</h3><p>Copy this code into the console to finish "
                f"adding the account.</p><p><code style='word-break:break-all'>"
                f"{_escape(code)}</code></p>"
                f"<p style='color:#666'>state {_escape(state)}</p>"
            )
            + "</body>"
        )

    # ---- console sign-in handoff ---------------------------------------------

    @app.post("/admin/console-ticket")
    async def console_ticket(request: Request) -> JSONResponse:
        """Mint a single-use ticket for the key that just authenticated.

        `tokenbiryani console` calls this with the admin key it read from the config,
        then opens the browser on the ticket. The key never travels in the URL; the
        ticket does, and it is spent the moment the page loads.
        """
        key = authenticate_admin(request)
        if not is_loopback(config.server.host):
            return _error(
                403,
                f"this gateway is bound to {config.server.host}, not loopback. A sign-in "
                "link is a bearer token in a URL, so it is minted only for a gateway "
                "nothing off-box can reach — sign in with the key instead.",
                "permission_error",
            )
        presented = _credential(request) or key.key
        try:
            ticket, ttl = app.state.tickets.mint(presented)
        except HandoffError as exc:
            return _error(409, str(exc), "invalid_request_error")
        return JSONResponse({"ticket": ticket, "expires_in": ttl})

    @app.post("/admin/console-session")
    async def console_session(request: Request) -> JSONResponse:
        """Redeem a ticket for the key it stands for.

        Deliberately keyless: the browser arriving here has nothing else to present,
        and the ticket is the credential. One redemption, then it is gone.
        """
        payload = await read_body(request)
        try:
            key = app.state.tickets.redeem(str(payload.get("ticket") or ""))
        except HandoffError as exc:
            return _error(401, str(exc), "authentication_error")
        return JSONResponse({"key": key})

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

    @app.patch("/admin/keys/{name}")
    async def update_key(request: Request, name: str) -> JSONResponse:
        """Edit a managed key's scope without reissuing it.

        Reissuing breaks every client already holding the key, which is why a key
        scoped at a since-deleted account usually just sits there blocking reload.
        """
        authenticate_admin(request)
        payload = await read_body(request)
        return JSONResponse(await app.state.gateway.update_key(name, **payload))

    @app.delete("/admin/keys/{name}")
    async def revoke_key(request: Request, name: str) -> JSONResponse:
        authenticate_admin(request)
        removed = await app.state.gateway.revoke_key(name)
        if not removed:
            return _error(404, f"no managed key named {name!r}", "not_found_error")
        return JSONResponse({"revoked": name})

    @app.get("/admin/settings")
    async def settings(request: Request) -> JSONResponse:
        authenticate_admin(request)
        return JSONResponse(app.state.gateway.settings_view())

    @app.post("/admin/settings")
    async def update_settings(request: Request) -> JSONResponse:
        """Change a gateway-wide setting from the console.

        Stored in the gateway, not written back to tokenbiryani.yaml: the file is
        the operator's, and a process that rewrites its operator's config file is
        one that eventually loses a comment somebody needed. The setting is applied
        again after every reload, so an unrelated edit to the file cannot revert it.
        """
        authenticate_admin(request)
        payload = await read_body(request)
        return JSONResponse(await app.state.gateway.update_settings(payload))

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
