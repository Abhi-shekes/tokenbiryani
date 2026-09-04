"""Command line: init, serve, status, strategies, keygen, doctor.

`status` ships at M1, long before the web console — the audience already lives in a
terminal, and building it first forces the event model into shape.

`doctor` is the odd one out: it is the only command that deliberately spends money,
because it is the only way to check the one assumption no mock can check for us —
that the real API spells its rate-limit headers the way the limit mirror expects.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG = "tokenbiryani.yaml"
DEFAULT_URL = "http://127.0.0.1:8787"

# Approximations of the console palette: ready / cooling / disabled / cache / brand.
C = {
    "ready": "\033[38;5;42m",
    "cooling": "\033[38;5;75m",
    "disabled": "\033[38;5;203m",
    "cache": "\033[38;5;141m",
    "brand": "\033[38;5;179m",
    "dim": "\033[38;5;244m",
    "bold": "\033[1m",
    "off": "\033[0m",
}


def _colors_enabled(force: Optional[bool] = None) -> bool:
    if force is not None:
        return force
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def paint(text: str, color: str, enabled: bool) -> str:
    if not enabled or color not in C:
        return text
    return C[color] + text + C["off"]


def _fmt_tokens(value: Optional[int]) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)


def _fmt_clock(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = int(max(0, seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _pct(fraction: Optional[float]) -> str:
    if fraction is None:
        return "—"
    return f"{fraction * 100:.0f}%"


def render_status(snapshot: Dict[str, Any], color: bool = True) -> str:
    pool = snapshot["pool"]
    stats = snapshot["stats"]
    lines: List[str] = []

    ready_tokens = _fmt_tokens(pool.get("ready_input_tokens"))
    cache_rate = stats.get("cache_hit_rate")
    lines.append(
        "  {}  {} tok ready {} next reset {} {} queue {} {} cache {}".format(
            paint("POOL", "dim", color),
            paint(ready_tokens, "bold", color),
            paint("·", "dim", color),
            paint(_fmt_clock(pool.get("next_reset_seconds")), "bold", color),
            paint("·", "dim", color),
            paint(str(snapshot["queue"]["depth"]), "bold", color),
            paint("·", "dim", color),
            paint(_pct(cache_rate), "cache", color),
        )
    )
    lines.append("")
    header = "  {:<10}{:<16}{:>6}{:>8}{:>9}{:>8}{:>8}".format(
        "ACCOUNT", "STATE", "REQ", "INPUT", "OUTPUT", "RESET", "CACHE"
    )
    lines.append(paint(header, "dim", color))

    for account in snapshot["accounts"]:
        state = account["state"]
        limits = account["limits"]
        bullet = paint("●", state if state in C else "dim", color)
        state_text = state
        if state == "cooling" and account.get("cooling_for") is not None:
            state_text = "cooling"
        note = ""
        if account.get("disabled_reason"):
            note = paint("  ← " + account["disabled_reason"], "dim", color)
        lines.append(
            "  {:<10}{} {:<14}{:>6}{:>8}{:>9}{:>8}{:>8}{}".format(
                account["id"],
                bullet,
                state_text,
                _pct(limits["requests"]["fraction"]),
                _pct(limits["input_tokens"]["fraction"]),
                _pct(limits["output_tokens"]["fraction"]),
                _fmt_clock(limits["input_tokens"]["reset_in"]),
                paint(_pct(account.get("cache_hit_rate")), "cache", color),
                note,
            )
        )

    lines.append("")
    lines.append(
        "  {}  {} requests {} {} failovers {} {} cache breaks {} {} errors {} {}".format(
            paint("1h", "dim", color),
            paint(str(stats["requests"]), "bold", color),
            paint("·", "dim", color),
            paint(str(stats["failovers"]), "ready", color),
            paint("·", "dim", color),
            paint(str(stats["cache_breaks"]), "cache", color),
            paint("·", "dim", color),
            paint(str(stats["errors"]), "disabled", color),
            paint("·", "dim", color),
            paint("${:.2f}".format(stats["spend_usd"]), "bold", color),
        )
    )
    return "\n".join(lines)


TEMPLATE = """\
# tokenbiryani — pooling gateway for Claude accounts
#
# Point your tools at this gateway instead of api.anthropic.com:
#   export ANTHROPIC_BASE_URL=http://{host}:{port}
#   export ANTHROPIC_AUTH_TOKEN={key}

server:
  host: {host}
  port: {port}

routing:
  # sticky_headroom keeps a conversation on the account holding its prompt cache.
  # Alternatives: headroom, cost_tiered, priority, least_loaded, round_robin
  strategy: sticky_headroom

store:
  # Persist affinity and the spend ledger so they survive a restart.
  backend: sqlite
  path: tokenbiryani.db

accounts:
  - id: acct-01
    type: anthropic_api
    api_key: ${{ANTHROPIC_API_KEY}}
    cost_tier: 1.0
  # - id: acct-02
  #   type: anthropic_api
  #   api_key: ${{ANTHROPIC_API_KEY_2}}
  #
  # Or add accounts from the console — no editing this file. Anything added there is
  # stored by the gateway, encrypted, and can be renamed, tested and rotated in place.

keys:
  - key: {key}
    name: default
    # Required to reach /admin/* — the pool snapshot, the request inspector, and
    # key management. Issue tenant keys without it.
    admin: true

# Costs are only reported, and spend caps only enforced, for models named here.
# USD per million tokens. Fill these in from Anthropic's current pricing page.
# pricing:
#   claude-opus-4-*:
#     input: 15.0
#     output: 75.0
#     cache_read: 1.5
#     cache_write: 18.75
"""


def cmd_init(args: argparse.Namespace) -> int:
    from .core.keys import generate_key

    path = args.config
    if os.path.exists(path) and not args.force:
        print(f"{path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    key = generate_key()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(TEMPLATE.format(host=args.host, port=args.port, key=key))
    color = _colors_enabled()
    print("wrote {}".format(paint(path, "brand", color)))
    print()
    print(f"  export ANTHROPIC_BASE_URL=http://{args.host}:{args.port}")
    print(f"  export ANTHROPIC_AUTH_TOKEN={key}")
    print("  export ANTHROPIC_API_KEY=sk-ant-...   # the real key the pool will use")
    print()
    print("  tokenbiryani serve")
    return 0


def cmd_strategies(args: argparse.Namespace) -> int:
    from .core.router import CUSTOM_STRATEGIES, available_strategies

    color = _colors_enabled()
    builtin = {
        "sticky_headroom": "affinity, then most headroom (default)",
        "headroom": "pure most-available; for stateless batch traffic",
        "cost_tiered": "drain cheap accounts first, spill upward",
        "priority": "strict ordered failover: primary, then backup",
        "least_loaded": "baseline",
        "round_robin": "baseline; ignores every signal on purpose",
    }
    for name in available_strategies():
        if name in builtin:
            note = builtin[name]
            origin = ""
        else:
            note = "custom scorer" if name in CUSTOM_STRATEGIES else "custom weights"
            origin = paint("  (plugin)", "brand", color)
        print("  {:<18}{}{}".format(name, paint(note, "dim", color), origin))
    return 0


def cmd_keygen(args: argparse.Namespace) -> int:
    from .core.keys import generate_key

    print(generate_key())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app
    from .config import Config, ConfigError

    try:
        config = Config.load(args.config)
    except FileNotFoundError:
        print(
            f"no {args.config} here. Run `tokenbiryani init` first.", file=sys.stderr
        )
        return 1
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    host = args.host or config.server.host
    port = args.port or config.server.port

    # A gateway holding real credentials must not be reachable off-box by accident.
    if host not in ("127.0.0.1", "localhost", "::1"):
        if not config.server.allow_remote:
            print(
                f"refusing to bind {host}: set server.allow_remote: true in {args.config} to serve "
                "off-loopback.",
                file=sys.stderr,
            )
            return 1
        if not config.keys:
            print(
                f"refusing to bind {host} with no keys configured — that would expose your "
                "credentials to the network. Add a `keys:` entry.",
                file=sys.stderr,
            )
            return 1

    if not config.accounts:
        print("config has no accounts; nothing to route to", file=sys.stderr)
        return 1

    app = create_app(config)
    color = _colors_enabled()
    print(
        "{} {} accounts · strategy {} · http://{}:{}".format(
            paint("tokenbiryani", "brand", color),
            len(config.accounts),
            config.routing.strategy,
            host,
            port,
        )
    )
    print(f"  console  http://{host}:{port}/console")
    print(f"  api      ANTHROPIC_BASE_URL=http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level=args.log_level)
    return 0


#: Every header the limit mirror reads. If a real response spells one of these
#: differently, that window stays empty, the account reads as full, and the router
#: silently degrades to round-robin — shredding the prompt cache while looking healthy.
EXPECTED_HEADERS = [
    "anthropic-ratelimit-{}-limit",
    "anthropic-ratelimit-{}-remaining",
    "anthropic-ratelimit-{}-reset",
]
LIMIT_WINDOWS = ["requests", "input-tokens", "output-tokens"]


def cmd_doctor(args: argparse.Namespace) -> int:
    """Send one real request upstream and report what it actually said.

    Everything in this project is tested against a mock that encodes assumptions
    about header spellings. This is the command that checks those assumptions
    against the real thing, which no test can do.
    """
    import time

    import httpx

    from .core.limits import LimitMirror

    color = _colors_enabled()
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    base_url = args.base_url

    if not api_key:
        # Fall back to the first API-key account in the config.
        try:
            from .config import Config

            config = Config.load(args.config)
        except Exception:
            config = None
        if config is not None:
            for account in config.accounts:
                if account.type == "anthropic_api" and account.api_key:
                    api_key = account.api_key
                    base_url = base_url or account.base_url
                    print("using account {} from {}".format(
                        paint(account.id, "brand", color), args.config))
                    break

    if not api_key:
        print(
            "doctor needs a real credential. Pass --api-key, set ANTHROPIC_API_KEY, or "
            "point --config at a file with an anthropic_api account.",
            file=sys.stderr,
        )
        return 1

    base_url = (base_url or "https://api.anthropic.com").rstrip("/")
    payload = {
        "model": args.model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }

    print()
    print("  {} {}".format(paint("POST", "dim", color), base_url + "/v1/messages"))
    print("  {} {}".format(paint("model", "dim", color), args.model))
    print("  {}".format(paint("one request, max_tokens=1 — a few cents at most", "dim", color)))
    print()

    try:
        response = httpx.post(
            base_url + "/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        print("  {} could not reach {}: {}".format(
            paint("FAIL", "disabled", color), base_url, exc), file=sys.stderr)
        return 1

    status_color = "ready" if response.status_code < 300 else "disabled"
    print("  {}  HTTP {}".format(
        paint("status", "dim", color), paint(str(response.status_code), status_color, color)))
    if response.status_code >= 300:
        print(f"  {response.text[:400]}")
        print()
        return 1

    lowered = {k.lower(): v for k, v in response.headers.items()}
    anthropic_headers = sorted(k for k in lowered if k.startswith("anthropic-"))

    print()
    print(paint("  headers the upstream actually returned", "dim", color))
    if not anthropic_headers:
        print("    {}".format(paint("none at all", "disabled", color)))
    for name in anthropic_headers:
        print(f"    {name:<44} {lowered[name]}")

    print()
    print(paint("  headers the limit mirror looks for", "dim", color))
    missing = []
    for window in LIMIT_WINDOWS:
        for template in EXPECTED_HEADERS:
            name = template.format(window)
            present = name in lowered
            if not present:
                missing.append(name)
            print("    {} {:<44} {}".format(
                paint("✓" if present else "✗", "ready" if present else "disabled", color),
                name,
                lowered.get(name, paint("missing", "disabled", color)),
            ))

    # The real proof: feed the response through the mirror the router reads.
    mirror = LimitMirror()
    mirror.update_from_headers(dict(response.headers), time.time())
    snapshot = mirror.snapshot(time.time())

    print()
    print(paint("  what the router would see", "dim", color))
    for label in ("requests", "input_tokens", "output_tokens"):
        parsed = snapshot[label]
        print("    {:<16} limit {:<10} remaining {:<10} resets {}".format(
            label,
            _fmt_tokens(parsed["limit"]),
            _fmt_tokens(parsed["remaining"]),
            _fmt_clock(parsed["reset_in"]),
        ))

    print()
    if missing:
        print("  {} {} of {} headers are missing or spelled differently.".format(
            paint("PROBLEM", "disabled", color), len(missing), len(LIMIT_WINDOWS) * 3))
        print("  Those windows stay empty, so those accounts read as full and routing")
        print("  degrades to round-robin. Please open an issue with the header list above.")
        print()
        return 1

    print("  {} every header the router needs is present and parsed.".format(
        paint("OK", "ready", color)))
    print()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    import httpx

    headers = {}
    token = args.key or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get(
        "TOKENBIRYANI_KEY"
    )
    if token:
        headers["x-api-key"] = token
    url = args.url.rstrip("/") + "/admin/status"
    try:
        response = httpx.get(url, headers=headers, timeout=5.0)
    except httpx.HTTPError as exc:
        print(f"cannot reach {args.url}: {exc}", file=sys.stderr)
        return 1
    if response.status_code == 401:
        print(
            "unauthorized — pass --key or set ANTHROPIC_AUTH_TOKEN", file=sys.stderr
        )
        return 1
    if response.status_code != 200:
        print(f"gateway returned {response.status_code}", file=sys.stderr)
        return 1
    snapshot = response.json()
    if args.json:
        print(json.dumps(snapshot, indent=2))
        return 0
    print()
    print(render_status(snapshot, color=_colors_enabled(None if not args.no_color else False)))
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokenbiryani",
        description="A pooling gateway for Claude accounts.",
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="path to config")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="write a starter config")
    init.add_argument("--host", default="127.0.0.1")
    init.add_argument("--port", type=int, default=8787)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)

    status = sub.add_parser("status", help="show the pool")
    status.add_argument("--url", default=os.environ.get("TOKENBIRYANI_URL", DEFAULT_URL))
    status.add_argument("--key")
    status.add_argument("--json", action="store_true")
    status.add_argument("--no-color", action="store_true")
    status.set_defaults(func=cmd_status)

    strategies = sub.add_parser("strategies", help="list routing strategies")
    strategies.set_defaults(func=cmd_strategies)

    doctor = sub.add_parser(
        "doctor",
        help="send one real request upstream and check the rate-limit headers",
    )
    doctor.add_argument("--api-key", help="defaults to $ANTHROPIC_API_KEY, then the config")
    doctor.add_argument("--base-url", help="defaults to https://api.anthropic.com")
    doctor.add_argument("--model", default="claude-sonnet-5")
    doctor.set_defaults(func=cmd_doctor)

    keygen = sub.add_parser("keygen", help="print a new virtual key")
    keygen.set_defaults(func=cmd_keygen)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
