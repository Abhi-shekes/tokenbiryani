"""Command line: init, serve, console, status, strategies, keygen, accounts, doctor.

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

#: How long to wait for open connections before closing them anyway.
#:
#: Without a bound, uvicorn waits forever, and this gateway always has a connection
#: that never ends: /admin/events is an SSE stream held open by every console tab.
#: A reload or a restart would then hang at "Waiting for connections to close" while
#: the port stayed open and answered nothing — which reads as a wedged gateway, and
#: fails a container healthcheck.
GRACEFUL_SHUTDOWN_SECONDS = 5

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

{accounts}
keys:
  - key: {key}
    name: default
    # Required to reach /admin/* — the pool snapshot, the request inspector, and
    # key management. Issue tenant keys without it.
    admin: true

# Costs are only reported, and spend caps only enforced, for models priced here.
# `builtin` uses the dated table that ships with this release — see the date on the
# Settings screen, and override any line by naming the model below it.
pricing: builtin
"""

#: What `init` writes when it has no credential to put in the file.
#:
#: Deliberately empty rather than a `${{ANTHROPIC_API_KEY}}` placeholder. Interpolation
#: raises on an unset variable — correctly, for a real deployment — so a template that
#: referenced one made `init && serve` fail on any machine that had not already
#: exported it. The console is where the first account belongs anyway.
EMPTY_ACCOUNTS = """\
accounts: []
  # Add accounts from the console at http://{host}:{port}/console — stored encrypted,
  # and renameable, testable and rotatable in place without touching this file.
  #
  # Or declare them here, and `tokenbiryani init --api-key sk-ant-…` will do it for you:
  #
  # - id: acct-01
  #   type: anthropic_api
  #   api_key: ${{ANTHROPIC_API_KEY}}
  #   cost_tier: 1.0
"""

#: And what it writes when `--api-key` gave it one.
SEEDED_ACCOUNTS = """\
accounts:
  - id: acct-01
    type: anthropic_api
    api_key: {api_key}
    cost_tier: 1.0
  # Add more from the console at http://{host}:{port}/console — no editing this file.
"""


def cmd_init(args: argparse.Namespace) -> int:
    from .core.keys import generate_key

    path = args.config
    if os.path.exists(path) and not args.force:
        print(f"{path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    key = generate_key()
    api_key = (args.api_key or "").strip()
    accounts = (
        SEEDED_ACCOUNTS.format(api_key=api_key, host=args.host, port=args.port)
        if api_key
        else EMPTY_ACCOUNTS.format(host=args.host, port=args.port)
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            TEMPLATE.format(host=args.host, port=args.port, key=key, accounts=accounts)
        )
    color = _colors_enabled()
    print("wrote {}".format(paint(path, "brand", color)))
    print()
    print("  tokenbiryani serve")
    print("  tokenbiryani console" + paint(
        "        opens the console signed in, and walks you through the first account",
        "dim", color))
    print()
    print(paint("  then point any Anthropic client at it:", "dim", color))
    print(f"  export ANTHROPIC_BASE_URL=http://{args.host}:{args.port}")
    print(f"  export ANTHROPIC_AUTH_TOKEN={key}")
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
    """Two different keys live behind one command, because operators confuse them.

    A virtual key is what clients authenticate with. A secret key encrypts account
    credentials before they reach the store, and it has to outlive the process that
    made it — lose it and every stored account is unrecoverable.
    """
    if getattr(args, "secret", False):
        from .core.secrets import SecretError
        from .core.secrets import generate_key as generate_secret

        try:
            print(generate_secret().decode("ascii"))
        except SecretError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    from .core.keys import generate_key

    print(generate_key())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app
    from .api.asgi import CONFIG_ENV
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

    # An empty pool used to be fatal, then a warning on stderr. It is neither: it is
    # the expected first run, and the wizard exists to walk through exactly this.
    # Printing it as a fault made the normal path look broken.
    empty_pool = not config.accounts

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
    print("  console  http://{}:{}/console{}".format(
        host, port,
        paint("   ·  `tokenbiryani console` opens it signed in", "dim", color)))
    print(f"  api      ANTHROPIC_BASE_URL=http://{host}:{port}")

    # Which keys this gateway will accept, masked. Enough to tell that the key in
    # your browser belongs to a different gateway — the failure mode when two dev
    # stacks with different keys are a `docker compose up` apart — and never enough
    # to use one, so it is safe in a log.
    if config.keys:
        from .core.keys import mask_key

        admins = [k for k in config.keys if k.admin]
        print("  keys     " + " · ".join(
            "{}{} {}".format(
                key.name,
                paint(" (admin)", "brand", color) if key.admin else "",
                paint(mask_key(key.key), "dim", color),
            )
            for key in config.keys
        ))
        if not admins:
            print(paint("           none of them is an admin key, so /console "
                        "cannot be used", "disabled", color))

    if empty_pool:
        print()
        print("  {} no accounts yet. Add your first one:".format(paint("next", "brand", color)))
        print("           tokenbiryani console" + paint(
            "      ← opens the console signed in", "dim", color))

    if args.reload:
        # The reloader re-imports the app in a fresh process, so it needs an import
        # string rather than the object we already built. The config path travels in
        # the environment because that new process does not inherit our argv.
        os.environ[CONFIG_ENV] = args.config
        print(paint("  reload   watching src/ for changes", "dim", color))
        uvicorn.run(
            "tokenbiryani.api.asgi:create",
            factory=True,
            host=host,
            port=port,
            log_level=args.log_level,
            reload=True,
            reload_dirs=args.reload_dir or None,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
        return 0

    uvicorn.run(
        create_app(config),
        host=host,
        port=port,
        log_level=args.log_level,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Send one real request upstream and report what it actually said.

    Everything in this project is tested against a mock that encodes assumptions
    about header spellings. This is the command that checks those assumptions
    against the real thing, which no test can do.
    """
    import httpx

    from .core.diagnostics import expected_headers, inspect_headers

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

    # One implementation of the check, shared with the console's verify step.
    report = inspect_headers(dict(response.headers))

    print()
    print(paint("  headers the upstream actually returned", "dim", color))
    if not report["returned"]:
        print("    {}".format(paint("none at all", "disabled", color)))
    for name, value in report["returned"].items():
        print(f"    {name:<44} {value}")

    print()
    print(paint("  headers the limit mirror looks for", "dim", color))
    for row in report["checked"]:
        print("    {} {:<44} {}".format(
            paint("✓" if row["present"] else "✗",
                  "ready" if row["present"] else "disabled", color),
            row["header"],
            row["value"] if row["present"] else paint("missing", "disabled", color),
        ))

    print()
    print(paint("  what the router would see", "dim", color))
    unified = report.get("family") == "unified"
    if unified:
        # A subscription session answers a different question: how much of each
        # rolling window is spent. There is no remaining count to print.
        for label, parsed in report["parsed"].items():
            print("    {:<16} {:<10} used {:<10} headroom {:<8} resets {}".format(
                label.replace("unified_", ""),
                parsed["status"] or "—",
                _pct(parsed["utilization"]),
                _pct(parsed["headroom"]),
                _fmt_clock(parsed["reset_in"]),
            ))
    else:
        for label, parsed in report["parsed"].items():
            print("    {:<16} limit {:<10} remaining {:<10} resets {}".format(
                label,
                _fmt_tokens(parsed["limit"]),
                _fmt_tokens(parsed["remaining"]),
                _fmt_clock(parsed["reset_in"]),
            ))

    print()
    if not report["ok"]:
        print("  {} {} of {} headers are missing or spelled differently.".format(
            paint("PROBLEM", "disabled", color),
            len(report["missing"]), len(expected_headers(unified))))
        if unified:
            print("  This account routes on assumed headroom rather than its real")
            print("  utilisation. Please open an issue with the header list above.")
        else:
            print("  Those windows stay empty, so those accounts read as full and routing")
            print("  degrades to round-robin. Please open an issue with the header list above.")
        print()
        return 1

    print("  {} every header the router needs is present and parsed{}.".format(
        paint("OK", "ready", color),
        " (subscription session: rolling windows)" if unified else ""))
    print()
    return 0


def _admin_key_from_config(config: Any) -> Optional[str]:
    """The first key in the file that can reach /admin. None if there is no such key."""
    for key in config.keys:
        if key.admin:
            return key.key
    return None


def cmd_console(args: argparse.Namespace) -> int:
    """Open the console in a browser, already signed in.

    The alternative — and what this replaces — was copying a `bir_…` key out of
    terminal scrollback and pasting it into a password field. That is the first
    thing a new user is asked to do, and it is the first thing that goes wrong.

    The key is never put in the URL. A single-use ticket is, and it is spent by the
    page before anything else happens.
    """
    import webbrowser

    import httpx

    from .config import Config, ConfigError

    color = _colors_enabled()
    key = args.key
    if not key:
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
        key = _admin_key_from_config(config)
        base = args.url or f"http://{config.server.host}:{config.server.port}"
    else:
        base = args.url or DEFAULT_URL
    base = base.rstrip("/")

    if not key:
        # An open gateway needs no ticket; anything else needs a key we do not have.
        print(f"  opening {base}/console")
        if not args.no_browser:
            webbrowser.open(base + "/console")
        print(paint(
            f"  no admin key in {args.config}, so this opens the sign-in page. Add one "
            "with `admin: true` under `keys:`.", "dim", color))
        return 0

    try:
        response = httpx.post(
            base + "/admin/console-ticket", headers={"x-api-key": key}, timeout=5.0
        )
    except httpx.HTTPError:
        print(
            f"cannot reach {base} — is the gateway running? Start it with "
            "`tokenbiryani serve`.",
            file=sys.stderr,
        )
        return 1

    if response.status_code == 401:
        print(f"the admin key in {args.config} was rejected by {base}. Two gateways with "
              "different keys is the usual cause.", file=sys.stderr)
        return 1
    if response.status_code != 200:
        # Bound off-loopback, most likely — the message says which.
        detail = ""
        try:
            detail = response.json().get("error", {}).get("message", "")
        except ValueError:
            detail = response.text[:200]
        print(f"{detail or response.status_code}", file=sys.stderr)
        return 1

    ticket = response.json().get("ticket", "")
    url = f"{base}/console?t={ticket}"
    print()
    print("  {} {}".format(paint("opening", "brand", color), base + "/console"))
    print(paint("  signed in with the admin key from " + args.config, "dim", color))
    if args.no_browser:
        print()
        print("  " + url)
        print(paint("  single use, and it expires in a minute", "dim", color))
    elif not webbrowser.open(url):
        print()
        print("  no browser to open. Use this link within the next minute:")
        print("  " + url)
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


# ---- accounts ------------------------------------------------------------------
# The console is not the only place a managed account should be reachable from.
# This audience lives in a terminal; a pool you can only edit in a browser is a
# worse tool for them, and these are four thin calls over the same admin API the
# console uses.


def _admin_request(args: argparse.Namespace, method: str, path: str, body=None):
    import httpx

    token = args.key or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get(
        "TOKENBIRYANI_KEY"
    )
    if not token:
        # Fall back to the config, the way `console` does — the key is right there.
        try:
            from .config import Config

            token = _admin_key_from_config(Config.load(args.config))
        except Exception:
            token = None
    url = args.url.rstrip("/") + path
    try:
        response = httpx.request(
            method, url,
            headers={"x-api-key": token} if token else {},
            json=body,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        print(f"cannot reach {args.url}: {exc}", file=sys.stderr)
        return None
    if response.status_code == 401:
        print("unauthorized — pass --key or set ANTHROPIC_AUTH_TOKEN", file=sys.stderr)
        return None
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message", "")
        except ValueError:
            detail = response.text[:200]
        print(detail or f"gateway returned {response.status_code}", file=sys.stderr)
        return None
    return response.json()


def cmd_accounts_list(args: argparse.Namespace) -> int:
    snapshot = _admin_request(args, "GET", "/admin/accounts")
    if snapshot is None:
        return 1
    if args.json:
        print(json.dumps(snapshot["accounts"], indent=2))
        return 0
    color = _colors_enabled()
    print()
    print(paint("  {:<20}{:<16}{:<12}{:>10}".format(
        "ACCOUNT", "TYPE", "STATE", "SPEND"), "dim", color))
    for account in snapshot["accounts"]:
        state = account["state"]
        print("  {:<20}{:<16}{} {:<10}{:>10}".format(
            account["id"],
            account.get("type", ""),
            paint("●", state if state in C else "dim", color),
            state,
            "${:.2f}".format(account.get("spend_usd") or 0.0),
        ))
    print()
    return 0


def cmd_accounts_add(args: argparse.Namespace) -> int:
    payload = {
        "id": args.id,
        "name": args.name or args.id,
        "type": args.type,
        "api_key": args.api_key or "",
        "cost_tier": args.cost_tier,
        "priority": args.priority,
    }
    if args.base_url:
        payload["base_url"] = args.base_url
    if args.spend_cap is not None:
        payload["spend_cap_usd"] = args.spend_cap

    color = _colors_enabled()
    # Test before storing, exactly as the console does: a wrong key should be a
    # message, not a disabled account somebody has to clean up later.
    if args.api_key and not args.no_test:
        probe = _admin_request(args, "POST", "/admin/accounts/test", {
            "api_key": args.api_key, "type": args.type, "base_url": args.base_url or "",
        })
        if probe is None:
            return 1
        if not probe.get("ok"):
            print("  {} the upstream rejected this credential: {}".format(
                paint("FAIL", "disabled", color), probe.get("detail")), file=sys.stderr)
            print("  nothing was stored. Pass --no-test to add it anyway.", file=sys.stderr)
            return 1
        print("  {} credential accepted in {:.2f}s".format(
            paint("ok", "ready", color), probe.get("latency") or 0.0))

    record = _admin_request(args, "POST", "/admin/accounts", payload)
    if record is None:
        return 1
    print("  {} added".format(paint(record["id"], "brand", color)))
    return 0


def cmd_accounts_test(args: argparse.Namespace) -> int:
    """Test one account, or every account, and report the header check too."""
    color = _colors_enabled()
    ids = [args.id] if args.id else None
    if ids is None:
        snapshot = _admin_request(args, "GET", "/admin/accounts")
        if snapshot is None:
            return 1
        ids = [a["id"] for a in snapshot["accounts"]]
    if not ids:
        print("no accounts to test", file=sys.stderr)
        return 1

    failed = 0
    unchecked = 0
    print()
    for account_id in ids:
        report = _admin_request(
            args, "POST", f"/admin/accounts/{account_id}/diagnose",
            {"spend": bool(args.deep)},
        )
        if report is None:
            failed += 1
            continue
        if not report.get("ok"):
            failed += 1
            print("  {} {:<20}{}".format(
                paint("✗", "disabled", color), account_id, report.get("detail")))
            continue

        source = report.get("limits_source")
        limits = report.get("limits")
        if source == "unobservable":
            print("  {} {:<20}credential ok · reports no limit headers by design".format(
                paint("●", "cooling", color), account_id))
        elif source == "not_observed":
            # Not a failure. The headers only arrive on a real completion, and none
            # has been served — saying "9 missing" here would be a false alarm.
            unchecked += 1
            print("  {} {:<20}credential ok · limit headers not seen yet".format(
                paint("?", "dim", color), account_id))
        elif limits and not limits.get("ok"):
            failed += 1
            print("  {} {:<20}credential ok, {} rate-limit header(s) missing".format(
                paint("!", "disabled", color), account_id, len(limits["missing"])))
            for name in limits["missing"]:
                print(paint("      " + name, "dim", color))
        else:
            print("  {} {:<20}credential ok · every limit header present".format(
                paint("✓", "ready", color), account_id))

    if unchecked and not args.deep:
        print()
        print(paint(
            "  the headers only arrive on a real completion. Send traffic through the "
            "gateway,\n  or re-run with --deep to spend one max_tokens=1 request per "
            "account.", "dim", color))
    print()
    return 1 if failed else 0


def cmd_accounts_rm(args: argparse.Namespace) -> int:
    result = _admin_request(
        args, "DELETE", "/admin/accounts/" + args.id
    )
    if result is None:
        return 1
    print("  {} removed".format(paint(args.id, "brand", _colors_enabled())))
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    parser = getattr(args, "_parser", None)
    if parser is not None:
        parser.print_help()
    return 1


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
    init.add_argument(
        "--api-key",
        help="seed the config with this credential. Without it the pool starts empty "
             "and the console's wizard adds the first account.",
    )
    init.set_defaults(func=cmd_init)

    console = sub.add_parser("console", help="open the console in a browser, signed in")
    console.add_argument("--url", help="defaults to the address in the config")
    console.add_argument("--key", help="defaults to the first admin key in the config")
    console.add_argument(
        "--no-browser", action="store_true", help="print the link instead of opening it"
    )
    console.set_defaults(func=cmd_console)

    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--log-level", default="info")
    serve.add_argument(
        "--reload",
        action="store_true",
        help="restart on source changes (development; needs the 'dev' extra)",
    )
    serve.add_argument(
        "--reload-dir",
        action="append",
        metavar="DIR",
        help="directory to watch; repeatable. Defaults to uvicorn's own choice.",
    )
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

    # --url and --key belong to every subcommand, not to `accounts` itself: defined
    # on the group, argparse only accepts them *before* the subcommand, which reads
    # as a bug the first time you type `accounts add x --url …`.
    reach = argparse.ArgumentParser(add_help=False)
    reach.add_argument("--url", default=os.environ.get("TOKENBIRYANI_URL", DEFAULT_URL))
    reach.add_argument("--key", help="defaults to the first admin key in the config")

    accounts = sub.add_parser("accounts", help="add, list, test and remove accounts")
    accounts.set_defaults(func=cmd_accounts, _parser=accounts)
    acct_sub = accounts.add_subparsers(dest="accounts_command")

    acct_list = acct_sub.add_parser("list", help="show the pool", parents=[reach])
    acct_list.add_argument("--json", action="store_true")
    acct_list.set_defaults(func=cmd_accounts_list)

    acct_add = acct_sub.add_parser(
        "add", help="add an account, testing it first", parents=[reach]
    )
    acct_add.add_argument("id")
    acct_add.add_argument("--api-key")
    acct_add.add_argument("--name")
    acct_add.add_argument("--type", default="anthropic_api")
    acct_add.add_argument("--base-url")
    acct_add.add_argument("--cost-tier", type=float, default=1.0)
    acct_add.add_argument("--priority", type=float, default=0.0)
    acct_add.add_argument("--spend-cap", type=float)
    acct_add.add_argument(
        "--no-test", action="store_true", help="store it without probing the credential"
    )
    acct_add.set_defaults(func=cmd_accounts_add)

    acct_test = acct_sub.add_parser(
        "test", help="probe a credential and check its rate-limit headers",
        parents=[reach],
    )
    acct_test.add_argument("id", nargs="?", help="omit to test every account")
    acct_test.add_argument(
        "--deep",
        action="store_true",
        help="spend one max_tokens=1 request per account to read its rate-limit "
             "headers, when no real traffic has done so yet",
    )
    acct_test.set_defaults(func=cmd_accounts_test)

    acct_rm = acct_sub.add_parser(
        "rm", help="remove a managed account", parents=[reach]
    )
    acct_rm.add_argument("id")
    acct_rm.set_defaults(func=cmd_accounts_rm)

    keygen = sub.add_parser("keygen", help="print a new virtual key")
    keygen.add_argument(
        "--secret",
        action="store_true",
        help="print a credential-encryption key for TOKENBIRYANI_SECRET_KEY instead",
    )
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
