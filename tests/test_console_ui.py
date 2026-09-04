"""Browser tests for the console.

These exist because of a bug static assertions could never catch: the request list
only fetched history when the SSE stream was *not* connected, so a freshly loaded
console showed nothing and the routing inspector — the headline feature — was
unreachable. Everything looked fine from the outside.

Skipped unless Playwright and a Chrome build are both present.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import httpx
import pytest
from conftest import body, build, make_config

playwright_api = pytest.importorskip("playwright.sync_api")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live(request):
    """A real HTTP gateway, with the mock wired in as a transport rather than a server."""
    import uvicorn

    from tokenbiryani.api.app import create_app
    from tokenbiryani.testing.mock_upstream import MockAnthropic

    mock = MockAnthropic()
    config = make_config(["acct-01", "acct-02"])
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
            if httpx.get(base + "/healthz", timeout=1).status_code:
                break
        except httpx.HTTPError:
            time.sleep(0.15)
    else:  # pragma: no cover
        pytest.fail("the gateway never came up")

    # Seed a few requests so every panel has something to render.
    for index in range(6):
        httpx.post(
            base + "/v1/messages", json=body(f"conversation-{index % 2}"),
            headers={"x-api-key": "bir_test"}, timeout=10,
        )
    httpx.post(base + "/v1/messages", json={"max_tokens": 1},
               headers={"x-api-key": "bir_test"}, timeout=10)

    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def page(live):
    with playwright_api.sync_playwright() as p:
        # CI installs Playwright's own chromium; a dev box usually has Chrome.
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
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.errors = []
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))
        page.goto(live + "/console", wait_until="networkidle")
        page.fill("#gate-key", "bir_test")
        page.click("#gate-go")
        page.wait_for_selector("#stream [data-request]", timeout=15000)
        yield page
        browser.close()


def test_the_request_list_populates_from_history(page):
    """The regression: history was only fetched when the live stream was down."""
    rows = page.query_selector_all("#stream [data-request]")
    assert len(rows) >= 6, "a freshly loaded console must show existing requests"


def test_the_inspector_opens_and_shows_the_decision(page):
    page.click("nav button[data-tab='requests']")
    page.wait_for_selector("#requests [data-request]")
    page.query_selector_all("#requests [data-request]")[0].click()
    page.wait_for_selector("#inspector .card", timeout=10000)
    text = page.inner_text("#inspector")
    assert "Routing decision" in text
    assert "chosen" in text, "the winning candidate must be marked"


def test_a_request_row_is_reachable_by_keyboard(page):
    page.click("nav button[data-tab='requests']")
    page.wait_for_selector("#requests [data-request]")
    row = page.query_selector_all("#requests [data-request]")[0]
    assert row.get_attribute("tabindex") == "0"
    row.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("#inspector .card", timeout=10000)


def test_countdowns_tick(page):
    page.click("nav button[data-tab='pool']")
    page.wait_for_selector(".cd")
    first = page.inner_text(".cd")
    page.wait_for_timeout(2300)
    assert page.inner_text(".cd") != first, "the countdown is the page's one moving part"


def test_the_pool_renders_its_parts(page):
    page.click("nav button[data-tab='pool']")
    page.wait_for_selector("#accounts-wrap tr[data-account]")
    assert len(page.query_selector_all("#stats .stat")) == 6
    assert page.query_selector_all("#hz div"), "capacity horizon drew no bars"
    assert page.query_selector("#accounts-wrap .spark"), "no latency sparkline"


def test_account_detail_opens_from_the_pool(page):
    page.click("nav button[data-tab='pool']")
    page.wait_for_selector("#accounts-wrap tr[data-account]")
    page.query_selector("#accounts-wrap tr[data-account]").click()
    page.wait_for_selector("#account-detail .card", timeout=10000)
    assert "acct-0" in page.inner_text("#account-detail")


def test_theme_and_density_toggle_and_persist(page):
    page.click("nav button[data-tab='pool']")
    page.click("#theme")
    assert page.get_attribute("html", "data-theme") == "light"
    page.click("#density")
    assert page.get_attribute("html", "data-density") == "compact"
    # Not networkidle: the console holds an open SSE stream, so it never goes idle.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#stream [data-request]", timeout=15000)
    assert page.get_attribute("html", "data-theme") == "light", "theme must survive a reload"
    assert page.get_attribute("html", "data-density") == "compact"
    page.click("#theme")
    page.click("#density")


def test_keys_can_be_minted_and_revoked_from_the_ui(page):
    page.click("nav button[data-tab='keys']")
    page.wait_for_selector("#keys tbody tr")
    page.fill("#k-name", "ui-minted")
    page.click("#k-create")
    page.wait_for_selector("#k-result .notice.ok", timeout=10000)
    assert "bir_" in page.inner_text("#k-result")
    page.wait_for_selector("[data-revoke='ui-minted']", timeout=10000)
    page.on("dialog", lambda d: d.accept())
    page.click("[data-revoke='ui-minted']")
    page.wait_for_timeout(1200)
    assert "ui-minted" not in page.inner_text("#keys")


def test_no_javascript_errors_anywhere(page):
    for tab in ("pool", "requests", "accounts", "keys", "settings"):
        page.click(f"nav button[data-tab='{tab}']")
        page.wait_for_timeout(250)
    assert page.errors == [], page.errors
