"""The console shell: served without a key, but every call it makes needs one."""

from __future__ import annotations

import httpx
from conftest import build, make_config

from tokenbiryani.api.app import create_app
from tokenbiryani.dashboard import console_html


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
    assert 'id="hz"' in html, "capacity horizon"
    assert 'id="inspector"' in html, "request inspector"
    assert 'id="gate"' in html, "first-run key gate"


def test_console_uses_the_validated_state_palette():
    html = console_html()
    for token in ("--ready:#2FA47A", "--cool:#4A85DE", "--crit:#DA6355", "--cache:#9068DE"):
        assert token in html, token
    # Both themes are defined, not one flipped.
    assert ':root[data-theme="light"]' in html
    assert "--ready:#12795B" in html


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

    from tokenbiryani.testing.mock_upstream import MockAccount

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
