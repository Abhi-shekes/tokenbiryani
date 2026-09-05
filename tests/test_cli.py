"""The CLI is the first interface; its renderer is worth a test."""

from __future__ import annotations

from tokenbiryani.cli import build_parser, render_status

SNAPSHOT = {
    "strategy": "sticky_headroom",
    "pool": {
        "total": 2,
        "ready": 1,
        "cooling": 1,
        "disabled": 0,
        "ready_input_tokens": 702_000,
        "next_reset_seconds": 12.0,
    },
    "queue": {"depth": 0, "max_size": 128, "highest_priority": None},
    "stats": {
        "requests": 412,
        "failovers": 3,
        "cache_breaks": 1,
        "errors": 0,
        "cache_hit_rate": 0.94,
        "spend_usd": 18.4,
    },
    "accounts": [
        {
            "id": "acct-01",
            "state": "ready",
            "disabled_reason": None,
            "cooling_for": None,
            "cache_hit_rate": 0.97,
            "limits": {
                "requests": {"fraction": 0.98, "reset_in": 41},
                "input_tokens": {"fraction": 0.82, "reset_in": 41},
                "output_tokens": {"fraction": 0.79, "reset_in": 41},
            },
        },
        {
            "id": "acct-03",
            "state": "cooling",
            "disabled_reason": None,
            "cooling_for": 27.0,
            "cache_hit_rate": None,
            "limits": {
                "requests": {"fraction": 0.04, "reset_in": 27},
                "input_tokens": {"fraction": 0.0, "reset_in": 27},
                "output_tokens": {"fraction": 0.06, "reset_in": 27},
            },
        },
    ],
}


def test_status_renders_without_color():
    output = render_status(SNAPSHOT, color=False)
    assert "702k tok ready" in output
    assert "00:12" in output
    assert "acct-01" in output and "ready" in output
    assert "acct-03" in output and "cooling" in output
    assert "94%" in output
    assert "\033[" not in output, "NO_COLOR output must contain no escape sequences"


def test_status_colors_states_when_enabled():
    output = render_status(SNAPSHOT, color=True)
    assert "\033[" in output


def test_missing_cache_rate_renders_as_a_dash():
    assert "—" in render_status(SNAPSHOT, color=False)


def test_parser_has_every_command():
    parser = build_parser()
    for command in ("init", "serve", "status", "keygen", "strategies", "doctor"):
        assert parser.parse_args([command]).command == command


def test_short_keys_are_masked_completely():
    from tokenbiryani.core.keys import generate_key, mask_key

    assert mask_key("bir_test") == "•" * 8
    assert "bir_test" not in mask_key("bir_test")
    long_key = generate_key()
    masked = mask_key(long_key)
    assert masked.endswith("…")
    assert len(masked) < len(long_key)
    assert long_key not in masked


# ---- doctor -------------------------------------------------------------------
# The one check no mock-backed test can make for you: does the real upstream spell
# the rate-limit headers the way the limit mirror expects?

FULL_HEADERS = {
    "anthropic-ratelimit-requests-limit": "1000",
    "anthropic-ratelimit-requests-remaining": "999",
    "anthropic-ratelimit-requests-reset": "2026-01-01T00:00:00Z",
    "anthropic-ratelimit-input-tokens-limit": "100000",
    "anthropic-ratelimit-input-tokens-remaining": "99000",
    "anthropic-ratelimit-input-tokens-reset": "2026-01-01T00:00:00Z",
    "anthropic-ratelimit-output-tokens-limit": "20000",
    "anthropic-ratelimit-output-tokens-remaining": "19000",
    "anthropic-ratelimit-output-tokens-reset": "2026-01-01T00:00:00Z",
}


def run_doctor(monkeypatch, headers, status_code=200):
    """Drive cmd_doctor against a scripted response, capturing what it prints."""
    import httpx

    from tokenbiryani import cli

    def fake_post(url, **kwargs):
        return httpx.Response(
            status_code=status_code,
            headers=headers,
            json={"type": "message", "content": []},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    args = build_parser().parse_args(
        ["doctor", "--api-key", "sk-ant-test", "--base-url", "http://upstream"]
    )
    return args, cli


def test_doctor_passes_when_every_header_is_present(monkeypatch, capsys):
    args, cli = run_doctor(monkeypatch, FULL_HEADERS)
    assert cli.cmd_doctor(args) == 0
    out = capsys.readouterr().out
    assert "every header the router needs is present" in out
    # It reports what the router would actually see, not just that keys exist.
    assert "100k" in out, "the parsed input-token limit"


def test_doctor_fails_loudly_when_a_header_is_spelled_differently(monkeypatch, capsys):
    """The exact silent failure TODO.md warns about: routing degrades, nothing errors."""
    renamed = dict(FULL_HEADERS)
    renamed.pop("anthropic-ratelimit-input-tokens-remaining")
    renamed["anthropic-ratelimit-input-token-remaining"] = "99000"   # note: singular

    args, cli = run_doctor(monkeypatch, renamed)
    assert cli.cmd_doctor(args) == 1, "a missing header must be a non-zero exit"
    out = capsys.readouterr().out
    assert "PROBLEM" in out
    assert "anthropic-ratelimit-input-tokens-remaining" in out
    # The header the upstream *did* send is shown, so the mismatch is diagnosable.
    assert "anthropic-ratelimit-input-token-remaining" in out


def test_doctor_reports_an_http_error_instead_of_a_traceback(monkeypatch, capsys):
    args, cli = run_doctor(monkeypatch, {}, status_code=401)
    assert cli.cmd_doctor(args) == 1
    assert "401" in capsys.readouterr().out


def test_serve_no_longer_refuses_an_empty_pool(tmp_path, monkeypatch, capsys):
    """An empty pool is a first run, not an error — the console is where you fix it.

    It used to exit 1, which left nowhere to add the first account from.
    """
    import tokenbiryani.cli as cli

    config = tmp_path / "tokenbiryani.yaml"
    config.write_text(
        "server: {host: 127.0.0.1, port: 8787}\n"
        "keys:\n  - {key: bir_empty_pool_test_000000, name: d, admin: true}\n"
    )

    served = {}
    monkeypatch.setitem(
        __import__("sys").modules, "uvicorn",
        type("uvicorn", (), {"run": staticmethod(lambda *a, **k: served.update(k))})(),
    )
    args = build_parser().parse_args(["-c", str(config), "serve"])
    assert cli.cmd_serve(args) == 0, "an empty pool must not be fatal"
    assert "add one at" in capsys.readouterr().err
    assert served, "the server still started"


def test_the_asgi_factory_builds_an_app_from_the_environment(tmp_path, monkeypatch):
    """uvicorn's reloader re-imports in a fresh process, so it needs this path."""
    from tokenbiryani.api.asgi import CONFIG_ENV, create

    config = tmp_path / "tokenbiryani.yaml"
    config.write_text(
        "server: {host: 127.0.0.1, port: 8787}\n"
        "accounts:\n  - {id: a, type: anthropic_api, api_key: k}\n"
        "keys:\n  - {key: bir_asgi_factory_test_00000, name: d, admin: true}\n"
    )
    monkeypatch.setenv(CONFIG_ENV, str(config))
    app = create()
    assert app.state.config.accounts[0].id == "a"


def test_keygen_mints_two_different_kinds_of_key(capsys):
    """Confusing them is easy and expensive, so they come from one command."""
    import tokenbiryani.cli as cli

    assert cli.cmd_keygen(build_parser().parse_args(["keygen"])) == 0
    virtual = capsys.readouterr().out.strip()
    assert virtual.startswith("bir_")

    assert cli.cmd_keygen(build_parser().parse_args(["keygen", "--secret"])) == 0
    secret = capsys.readouterr().out.strip()
    assert not secret.startswith("bir_")
    # A Fernet key, which is what TOKENBIRYANI_SECRET_KEY has to be.
    from cryptography.fernet import Fernet

    Fernet(secret.encode())


def fake_uvicorn(monkeypatch, served):
    import sys

    monkeypatch.setitem(
        sys.modules, "uvicorn",
        type("uvicorn", (), {"run": staticmethod(lambda *a, **k: served.update(k))})(),
    )


def test_serve_says_which_keys_this_gateway_accepts(tmp_path, monkeypatch, capsys):
    """Two dev stacks a `docker compose up` apart have different keys, and the only
    symptom of using the wrong one is a 401. Masked names in the banner make it
    obvious which gateway you are actually looking at.
    """
    import tokenbiryani.cli as cli

    config = tmp_path / "tokenbiryani.yaml"
    config.write_text(
        "server: {host: 127.0.0.1, port: 8787}\n"
        "accounts:\n  - {id: a, type: anthropic_api, api_key: k}\n"
        "keys:\n"
        "  - {key: bir_dev_only_change_me_0123456789, name: default, admin: true}\n"
        "  - {key: bir_tenant_readonly_key_000111222, name: tenant}\n"
    )
    fake_uvicorn(monkeypatch, {})
    assert cli.cmd_serve(build_parser().parse_args(["-c", str(config), "serve"])) == 0

    out = capsys.readouterr().out
    assert "default" in out and "tenant" in out
    assert "(admin)" in out
    # Enough to tell two keys apart, never enough to use one.
    assert "bir_dev_only_change_me_0123456789" not in out
    assert "bir_tenant_readonly_key_000111222" not in out
    assert "…" in out


def test_serve_warns_when_no_key_can_reach_the_console(tmp_path, monkeypatch, capsys):
    import tokenbiryani.cli as cli

    config = tmp_path / "tokenbiryani.yaml"
    config.write_text(
        "server: {host: 127.0.0.1, port: 8787}\n"
        "accounts:\n  - {id: a, type: anthropic_api, api_key: k}\n"
        "keys:\n  - {key: bir_tenant_readonly_key_000111222, name: tenant}\n"
    )
    fake_uvicorn(monkeypatch, {})
    cli.cmd_serve(build_parser().parse_args(["-c", str(config), "serve"]))
    assert "cannot be used" in capsys.readouterr().out


def test_serve_bounds_its_graceful_shutdown(tmp_path, monkeypatch):
    """This gateway always holds a connection that never ends.

    /admin/events is an SSE stream, open for as long as a console tab is. Without a
    bound, uvicorn's graceful shutdown waits for it forever: a reload or a restart
    hangs at "Waiting for connections to close" with the port still open and
    answering nothing, which reads as a wedged gateway and fails a healthcheck.
    """
    import tokenbiryani.cli as cli

    config = tmp_path / "tokenbiryani.yaml"
    config.write_text(
        "server: {host: 127.0.0.1, port: 8787}\n"
        "accounts:\n  - {id: a, type: anthropic_api, api_key: k}\n"
        "keys:\n  - {key: bir_graceful_test_0123456789, name: d, admin: true}\n"
    )

    for argv in (["serve"], ["serve", "--reload"]):
        served = {}
        fake_uvicorn(monkeypatch, served)
        assert cli.cmd_serve(build_parser().parse_args(["-c", str(config)] + argv)) == 0
        assert served["timeout_graceful_shutdown"] == cli.GRACEFUL_SHUTDOWN_SECONDS, argv
    assert 0 < cli.GRACEFUL_SHUTDOWN_SECONDS <= 30
