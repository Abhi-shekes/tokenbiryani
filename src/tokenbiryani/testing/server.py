"""Run the mock upstream as a real HTTP server.

The in-process transport covers unit tests; this exists so the gateway can be smoke
tested end to end over real sockets, and so contributors can point a running gateway
at a fake Anthropic without spending money.

    python -m tokenbiryani.testing.server --port 9911 --accounts key-a,key-b
"""

from __future__ import annotations

import argparse
import json
from typing import Optional

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .mock_upstream import MockAnthropic


def create_mock_app(mock: Optional[MockAnthropic] = None) -> Starlette:
    mock = mock or MockAnthropic()

    async def anthropic(request: Request) -> Response:
        raw = await request.body()
        upstream_request = httpx.Request(
            request.method,
            str(request.url),
            headers=dict(request.headers),
            content=raw,
        )
        try:
            result = mock.handle(upstream_request)
        except httpx.HTTPError as exc:
            # A scripted transport failure has no HTTP equivalent; the closest a real
            # server can do is refuse to answer.
            return JSONResponse(
                {"type": "error", "error": {"type": "api_error", "message": str(exc)}},
                status_code=502,
            )

        headers = {
            k: v for k, v in result.headers.items() if k.lower() not in ("content-length",)
        }
        try:
            streaming = json.loads(raw or b"{}").get("stream")
        except ValueError:
            streaming = False

        if streaming and result.status_code == 200:
            return StreamingResponse(
                result.aiter_bytes(),
                status_code=result.status_code,
                headers=headers,
                media_type="text/event-stream",
            )
        return Response(
            content=result.content, status_code=result.status_code, headers=headers
        )

    async def models(request: Request) -> Response:
        return JSONResponse({"data": [{"id": "claude-test-1", "type": "model"}]})

    app = Starlette(
        routes=[
            Route("/v1/messages", anthropic, methods=["POST"]),
            Route("/v1/messages/count_tokens", anthropic, methods=["POST"]),
            Route("/v1/models", models, methods=["GET"]),
        ]
    )
    app.state.mock = mock
    return app


def main() -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="Mock Anthropic API for smoke tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9911)
    parser.add_argument(
        "--accounts",
        default="key-a,key-b",
        help="comma-separated api keys this mock will accept",
    )
    parser.add_argument("--input-limit", type=int, default=100_000)
    parser.add_argument(
        "--cache",
        action="store_true",
        help="model the per-credential prompt cache, so cache-hit rate is a real "
             "number rather than a flat zero. Off by default because scripted "
             "behaviours stay exact without it.",
    )
    args = parser.parse_args()

    mock = MockAnthropic()
    for index, api_key in enumerate(args.accounts.split(",")):
        mock.add(
            f"acct-{index + 1:02d}", api_key.strip(),
            input_limit=args.input_limit, cache_aware=args.cache,
        )

    uvicorn.run(create_mock_app(mock), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
