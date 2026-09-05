"""The console shell: served without a key, but every call it makes needs one."""

from __future__ import annotations

import httpx
from conftest import build, make_config

from tokenbiryani.api.app import create_app
from tokenbiryani.dashboard import console_css, console_html


def client_for(mock, account_ids=("a", "b")):
    config = make_config(list(account_ids))
    gateway = build(mock, config)
    app = create_app(config, gateway)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


async def test_console_is_served_without_a_key(mock):
    """The shell carries no data; the key it later uses never leaves the browser."""
    async with client_for(mock) as client:
        response = await client.get("/console")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "tokenbiryani console" in response.text


async def test_root_redirects_to_the_console(mock):
    async with client_for(mock) as client:
        response = await client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/console"


async def test_the_shell_ships_no_credentials_or_data(mock):
    async with client_for(mock) as client:
        body = (await client.get("/console")).text
    assert "bir_test" not in body, "the served page must never contain a key"
    assert "key-a" not in body, "nor an upstream credential"


def test_console_covers_every_designed_surface():
    html = console_html()
    for panel in ("pool", "requests", "accounts", "keys", "settings"):
        assert f'data-panel="{panel}"' in html
    # The screens the UI design calls for, by their distinguishing element.
    assert 'data-panel="guide"' in html, "connect-a-client guide"
    assert 'id="hz"' in html, "capacity horizon"
    assert 'id="inspector"' in html, "request inspector"
    assert 'id="gate"' in html, "first-run key gate"


def test_console_uses_the_validated_state_palette():
    """The palette lives in the stylesheet now; the values themselves are unchanged."""
    css = console_css()
    for token in ("--ready:#2FA47A", "--cool:#4A85DE", "--crit:#DA6355", "--cache:#9068DE"):
        assert token in css, token
    # Both themes are defined, not one flipped.
    assert ':root[data-theme="light"]' in css
    assert "--ready:#12795B" in css


def test_console_reads_events_with_fetch_not_eventsource():
    """EventSource cannot set headers, so an admin key would end up in the URL."""
    html = console_html()
    assert "new EventSource" not in html, "the comment may name it; the code must not use it"
    assert 'fetch("/admin/events"' in html


async def test_console_calls_are_authenticated(mock):
    """Whatever the shell asks for, the API still demands a key."""
    async with client_for(mock) as client:
        for path in ("/admin/status", "/admin/horizon", "/admin/requests", "/admin/keys"):
            assert (await client.get(path)).status_code == 401


async def test_the_mock_window_rolls_so_countdowns_mean_something(mock):
    """A frozen reset timestamp makes every countdown in the console read 'now'."""
    import time

    from support.mock_upstream import MockAccount

    account = MockAccount(api_key="k", window_seconds=60.0)
    account.input_remaining = 5
    past = time.time() - 1
    account.reset_at = past

    account.roll_window()

    assert account.reset_at > time.time(), "the window must move into the future"
    assert account.input_remaining == account.input_limit, "and refill on the way"


async def test_a_rolled_window_shows_a_real_countdown(gateway_factory, mock, key):
    import time

    from conftest import body

    gateway = gateway_factory(["a"])
    mock.accounts["a"].reset_at = time.time() - 1     # already expired
    await gateway.complete(body(), {}, key)

    reset_in = gateway.accounts["a"].mirror.input_tokens.seconds_to_reset(time.time())
    assert reset_in is not None and reset_in > 0, "the console would show 'now' forever"


def test_the_landing_page_says_what_this_is_before_asking_for_a_key():
    """An unauthenticated visitor used to get a bare box. Now they get a page."""
    html = console_html()
    assert 'class="lp ' in html, "the landing page shell"
    assert "Pooling gateway for Claude accounts" in html
    assert 'id="gate-key"' in html, "and the key field is still on it"
    assert 'id="gate-go"' in html
    # The value proposition, not just a form.
    assert "1.94x" in html, "the claim the project is built on"


def test_the_mark_is_drawn_not_an_emoji():
    """An emoji in a coloured square is not a logo, and it dies at 16px."""
    html = console_html()
    assert "&#127835;" not in html, "the curry emoji is gone"
    assert 'class="mark"' in html
    # Same silhouette in the favicon, so the tab and the header agree.
    assert "M6 10.5h20v10.5" in html
    assert html.count("M6 10.5h20v10.5") >= 4, "favicon plus every lockup"


def test_the_console_files_are_re_read_when_they_change(tmp_path, monkeypatch):
    """Caching outright meant editing the console and refreshing showed the old page.

    That bit under `serve --reload`, which watches Python files but not these, and
    in a container with the source bind-mounted.
    """
    import os
    import time

    from tokenbiryani import dashboard

    path = tmp_path / "console.html"
    path.write_text("first")
    monkeypatch.setattr(dashboard, "CONSOLE_PATH", str(path))
    monkeypatch.setattr(dashboard, "_cache", {})

    assert dashboard.console_html() == "first"
    assert dashboard.console_html() == "first", "unchanged files are still cached"

    path.write_text("second")
    os.utime(path, (time.time() + 2, time.time() + 2))
    assert dashboard.console_html() == "second", "a changed file is re-read"


# ---- signing in from the terminal ---------------------------------------------
# The console's first screen used to be a password field fed by copy-paste out of
# scrollback. `tokenbiryani console` replaces that with a handoff, and the property
# that makes the handoff safe is that the ticket — not the key — is what travels.


async def test_a_ticket_is_exchanged_for_the_key_exactly_once(mock):
    async with client_for(mock) as client:
        minted = await client.post(
            "/admin/console-ticket", headers={"x-api-key": "bir_test"}
        )
        assert minted.status_code == 200
        ticket = minted.json()["ticket"]

        first = await client.post("/admin/console-session", json={"ticket": ticket})
        assert first.status_code == 200
        assert first.json()["key"] == "bir_test"

        replayed = await client.post("/admin/console-session", json={"ticket": ticket})
        assert replayed.status_code == 401, "a spent ticket is worth nothing"


async def test_a_ticket_needs_an_admin_key_to_mint(mock):
    async with client_for(mock) as client:
        assert (await client.post("/admin/console-ticket")).status_code == 401
        rejected = await client.post(
            "/admin/console-ticket", headers={"x-api-key": "bir_wrong"}
        )
        assert rejected.status_code == 401


async def test_no_ticket_is_minted_for_a_gateway_the_network_can_reach(mock):
    """A ticket is a bearer token in a URL. That is only ever acceptable on loopback."""
    config = make_config(["a"])
    config.server.host = "0.0.0.0"
    config.server.allow_remote = True
    app = create_app(config, build(mock, config))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gw"
    ) as client:
        response = await client.post(
            "/admin/console-ticket", headers={"x-api-key": "bir_test"}
        )
    assert response.status_code == 403
    assert "loopback" in response.json()["error"]["message"]


async def test_an_unknown_ticket_is_refused(mock):
    async with client_for(mock) as client:
        response = await client.post("/admin/console-session", json={"ticket": "made-up"})
    assert response.status_code == 401
