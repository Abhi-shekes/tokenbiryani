"""The first run, driven end to end in a browser.

This is the activation funnel: an empty pool, and the three steps between it and a
working client. It gets its own module because it needs a gateway with *no* accounts,
which is the one thing every other console test assumes it does not have.

Three of the things asserted here were broken before, and none of them were visible to
a unit test:

* steps 2 and 3 never ran at all — ``refresh()`` cleared the wizard's state the
  instant the pool stopped being empty, which is exactly when they were due;
* the last step handed over the literal string ``<your key>`` while the browser held
  a working key in ``localStorage``;
* the verify step reported all nine rate-limit headers missing for a healthy account,
  because it read them from a ``/v1/models`` probe, which carries none.

Skipped unless Playwright and a Chrome build are both present.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import httpx
import pytest
from conftest import build, make_config

playwright_api = pytest.importorskip("playwright.sync_api")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def empty(tmp_path_factory):
    """A real HTTP gateway with nothing in the pool: the state a new install is in."""
    import uvicorn
    from support.mock_upstream import MockAnthropic

    from tokenbiryani.api.app import create_app

    mock = MockAnthropic()
    mock.add("added-from-the-ui", "key-a")
    config = make_config([], overrides={"pricing": {"claude-test-*": {
        "input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75,
    }}})
    config.store.secret_key_path = str(tmp_path_factory.mktemp("secrets") / "secret.key")
    gateway = build(mock, config)
    app = create_app(config, gateway)
    port = free_port()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(base + "/console", timeout=1).status_code:
                break
        except httpx.HTTPError:
            time.sleep(0.15)

    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def wizard(empty):
    """Arrive the way `tokenbiryani console` arrives: on a single-use ticket."""
    ticket = httpx.post(
        empty + "/admin/console-ticket", headers={"x-api-key": "bir_test"}, timeout=10
    ).json()["ticket"]

    with playwright_api.sync_playwright() as p:
        launches = [{"channel": "chrome"}, {}]
        if os.environ.get("TOKENBIRYANI_UI_BROWSER") == "chromium":
            launches.reverse()
        browser = None
        for options in launches:
            try:
                browser = p.chromium.launch(**options)
                break
            except Exception:  # pragma: no cover - depends on the machine
                continue
        if browser is None:
            pytest.skip("no chromium or chrome build available")

        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.errors = []
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))
        # Not networkidle: a signed-in console holds /admin/events open forever.
        page.goto(f"{empty}/console?t={ticket}", wait_until="domcontentloaded")
        yield page
        browser.close()


def test_the_whole_first_run(wizard):
    """One test, because it is one flow: a user does not stop halfway.

    Split into per-step tests it would need the wizard re-driven from scratch each
    time, and the thing most worth protecting — that the steps actually follow one
    another — is exactly what that would stop checking.
    """
    page = wizard

    # ---- step 1: the ticket lands on the wizard, not on a password field --------
    page.wait_for_selector("#onboarding:not(.hidden)", timeout=15000)
    assert "?t=" not in page.url, "the ticket is spent and out of the address bar"

    # The rail states the whole shape of the setup before any of it is asked for.
    assert page.eval_on_selector_all(".wiz-step", "els => els.length") == 4
    assert page.eval_on_selector_all(
        ".wiz-step", "els => els.filter(e => e.classList.contains('on')).length"
    ) == 1, "exactly one step is current"

    page.check('input[name="ob-route"][value="anthropic_api"]')
    page.click("#ob-add")

    # ---- step 2: the credential, on the page rather than in a dialog -----------
    page.wait_for_selector("#ob-form [data-field='api_key']", timeout=15000)
    assert not page.query_selector(".modal"), (
        "the form is mounted in the page; a wizard whose first step opens a dialog "
        "is a wizard that does nothing itself"
    )
    assert page.eval_on_selector_all(
        ".wiz-step", "els => els.filter(e => e.classList.contains('done')).length"
    ) == 1, "step 1 is marked done once it is behind you"

    page.fill("#m-name", "My First Account")
    page.fill("#m-fields [data-field='api_key']", "key-a")

    page.click("#ob-form .adv > summary")
    assert page.input_value("#m-id") == "my-first-account", (
        "the id is derived from the name, not demanded from the user"
    )
    page.click("#m-save-test")

    # ---- step 3: verify --------------------------------------------------------
    page.wait_for_selector("#ob-next", timeout=20000)
    body = page.inner_text("#ob-body")
    assert "is in the pool" in body, "step 3 renders — it used to be unreachable"
    assert "Credential accepted" in body
    assert "missing or spelled differently" not in body, (
        "no false alarm: /v1/models carries no rate-limit headers, and reporting all "
        "nine missing about a healthy pool is worse than not checking"
    )
    assert page.query_selector("#ob-spend"), "it offers the paid check instead"

    page.click("#ob-spend")
    page.wait_for_selector("#ob-next", timeout=20000)
    assert "every rate-limit header the router needs is present" in page.inner_text("#ob-body"), (
        "and once one real completion has run, the check gives a real verdict"
    )

    # ---- step 4: connect -------------------------------------------------------
    page.click("#ob-next")
    page.wait_for_selector("#ob-connect .code", timeout=15000)
    code = page.inner_text("#ob-connect .code")
    assert "bir_test" in code, "a key that works"
    assert "your key" not in code, "not a placeholder"

    page.click("#ob-mint")
    page.wait_for_function("() => state.mintedKey", timeout=15000)
    minted = page.evaluate("() => state.mintedKey")
    assert minted in page.inner_text("#ob-connect .code"), (
        "and a client key without admin, which is what a client should carry"
    )

    page.click("#ob-go")
    page.wait_for_selector("#app:not(.hidden)", timeout=15000)

    # ---- and it is reachable again, which is the only way it gets reviewed -----
    # Keyed off an empty pool alone, this screen was visible exactly once in the
    # life of an install. Adding the second account should not mean editing YAML.
    page.click("#nav-setup")
    page.wait_for_selector("#onboarding:not(.hidden)", timeout=15000)
    page.wait_for_selector(".routes", timeout=15000)
    assert "already has" in page.inner_text("#ob-body"), (
        "re-entered from a working console, it must not imply the pool is empty"
    )
    assert page.inner_text("#ob-skip") == "Back to the console"
    page.click("#ob-skip")
    page.wait_for_selector("#app:not(.hidden)", timeout=15000)

    assert not page.errors, page.errors
