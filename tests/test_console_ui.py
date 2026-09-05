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
    page.click(".nav button[data-tab='requests']")
    page.wait_for_selector("#requests [data-request]")
    page.query_selector_all("#requests [data-request]")[0].click()
    page.wait_for_selector("#inspector .card", timeout=10000)
    text = page.inner_text("#inspector")
    assert "Routing decision" in text
    assert "chosen" in text, "the winning candidate must be marked"


def test_a_request_row_is_reachable_by_keyboard(page):
    page.click(".nav button[data-tab='requests']")
    page.wait_for_selector("#requests [data-request]")
    row = page.query_selector_all("#requests [data-request]")[0]
    assert row.get_attribute("tabindex") == "0"
    row.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("#inspector .card", timeout=10000)


def test_countdowns_tick(page):
    page.click(".nav button[data-tab='pool']")
    page.wait_for_selector(".cd")
    first = page.inner_text(".cd")
    page.wait_for_timeout(2300)
    assert page.inner_text(".cd") != first, "the countdown is the page's one moving part"


def test_the_pool_renders_its_parts(page):
    page.click(".nav button[data-tab='pool']")
    page.wait_for_selector("#accounts-wrap tr[data-account]")
    assert len(page.query_selector_all("#stats .tile")) == 6
    assert page.query_selector_all("#hz div"), "capacity horizon drew no bars"
    assert page.query_selector("#accounts-wrap .spark"), "no latency sparkline"


def test_account_detail_opens_from_the_pool(page):
    page.click(".nav button[data-tab='pool']")
    page.wait_for_selector("#accounts-wrap tr[data-account]")
    page.query_selector("#accounts-wrap tr[data-account]").click()
    page.wait_for_selector("#account-detail .card", timeout=10000)
    assert "acct-0" in page.inner_text("#account-detail")


def test_theme_and_density_toggle_and_persist(page):
    page.click(".nav button[data-tab='pool']")
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
    page.click(".nav button[data-tab='keys']")
    page.wait_for_selector("#keys tbody tr")
    page.fill("#k-name", "ui-minted")
    page.click("#k-create")
    page.wait_for_selector("#k-result .note.ok", timeout=10000)
    assert "bir_" in page.inner_text("#k-result")
    page.wait_for_selector("[data-revoke='ui-minted']", timeout=10000)
    page.on("dialog", lambda d: d.accept())
    page.click("[data-revoke='ui-minted']")
    page.wait_for_timeout(1200)
    assert "ui-minted" not in page.inner_text("#keys")


def test_no_javascript_errors_anywhere(page):
    for tab in ("pool", "usage", "requests", "accounts", "keys", "guide", "settings"):
        page.click(f".nav button[data-tab='{tab}']")
        page.wait_for_timeout(250)
    assert page.errors == [], page.errors


def test_an_account_can_be_added_named_and_deleted_from_the_ui(page):
    """The gap this phase closes: the managed-account API had no UI at all."""
    page.click(".nav button[data-tab='accounts']")
    page.wait_for_selector("#accounts-table tr[data-row]")

    page.click("#acct-add")
    page.wait_for_selector(".modal")
    page.fill("#m-name", "Spare capacity")
    page.fill("#m-id", "acct-ui")
    page.fill("#m-fields [data-field='api_key']", "key-from-the-ui")
    page.click("#m-save")

    page.wait_for_selector("tr[data-row='acct-ui']", timeout=10000)
    row = page.inner_text("tr[data-row='acct-ui']")
    assert "Spare capacity" in row, "the name it was given must be what the table shows"
    assert "managed" in row.lower(), "and it must read as editable, not config-owned"

    # Renaming goes through PATCH and survives the next poll.
    page.click("[data-edit='acct-ui']")
    page.wait_for_selector(".modal")
    page.fill("#m-name", "Renamed in place")
    page.click("#m-save")
    page.wait_for_function(
        "document.querySelector(\"tr[data-row='acct-ui']\")"
        "?.innerText.includes('Renamed in place')",
        timeout=10000,
    )

    page.on("dialog", lambda d: d.accept())
    page.click("[data-del='acct-ui']")
    page.wait_for_function(
        "!document.querySelector(\"tr[data-row='acct-ui']\")", timeout=10000
    )


def test_a_credential_can_be_tested_from_the_ui(page):
    """'Did it actually connect' has to be answerable without reading a log."""
    page.click(".nav button[data-tab='accounts']")
    page.wait_for_selector("#accounts-table tr[data-row]")
    page.click("[data-test='acct-01']")
    # Not wait_for_selector: an earlier test may have left a message in this slot.
    page.wait_for_function(
        "document.querySelector('#acct-msg .note')?.innerText.includes('acct-01')",
        timeout=15000,
    )


def test_config_accounts_are_locked_against_editing(page):
    """The file is the operator's. The UI must not offer to overwrite it."""
    page.click(".nav button[data-tab='accounts']")
    page.wait_for_selector("#accounts-table tr[data-row='acct-01']")
    row = page.query_selector("tr[data-row='acct-01']")
    assert row.query_selector("[data-del]") is None, "config accounts get no Delete"
    assert row.query_selector("[data-edit]") is None, "config accounts get no Edit"
    assert row.query_selector("[data-test]"), "but testing one is always allowed"


def test_the_usage_screen_draws_its_charts(page):
    page.click(".nav button[data-tab='usage']")
    page.wait_for_selector("#u-tokens svg", timeout=15000)
    assert page.query_selector_all("#u-tokens .seg"), "no stacked segments"
    assert page.query_selector("#u-cost svg"), "no cost chart"
    assert page.query_selector("#u-cache svg"), "no cache-rate chart"
    # Every value the charts encode as colour is also readable as a number.
    assert page.query_selector("#u-table table"), "the table view is the contrast relief"
    assert page.query_selector("#u-tokens .legend"), "two or more series need a legend"


def test_changing_the_range_refetches_every_chart(page):
    page.click(".nav button[data-tab='usage']")
    page.wait_for_selector("#u-tokens svg", timeout=15000)
    before = page.inner_text("#u-note")
    page.click("[data-window='7d']")
    page.wait_for_function(
        f"document.querySelector('#u-note').innerText !== {before!r}", timeout=15000
    )
    assert page.query_selector_all("#u-tokens .seg"), "charts must survive a range change"
    page.click("[data-window='24h']")
    page.wait_for_timeout(600)


def test_series_colour_follows_the_entity_not_its_rank(page):
    """A reader who learned 'acct-01 is blue' must not be lied to by a re-sort."""
    page.click(".nav button[data-tab='usage']")
    page.wait_for_selector("#u-tokens svg", timeout=15000)
    slots = page.evaluate("JSON.stringify(state.slots)")
    page.click("[data-window='7d']")
    page.wait_for_timeout(900)
    page.click("[data-group='model']")
    page.wait_for_timeout(900)
    page.click("[data-group='account']")
    page.wait_for_timeout(900)
    after = page.evaluate("JSON.stringify(state.slots)")
    import json
    for name, slot in json.loads(slots).items():
        assert json.loads(after)[name] == slot, f"{name} was repainted"
    page.click("[data-window='24h']")
    page.wait_for_timeout(600)


def test_a_chart_hover_shows_the_bucket(page):
    page.click(".nav button[data-tab='usage']")
    page.wait_for_selector("#u-tokens [data-bucket]", timeout=15000)
    page.hover("#u-tokens [data-bucket='5']")
    page.wait_for_selector("#u-tokens .tip:not(.hidden)", timeout=5000)
    assert page.inner_text("#u-tokens .tip").strip()


def test_the_subscription_type_offers_a_login_not_a_credential_field(page):
    """A subscription is signed into, so the modal must not ask for a key."""
    page.click(".nav button[data-tab='accounts']")
    page.wait_for_selector("#accounts-table tr[data-row]")
    page.click("#acct-add")
    page.wait_for_selector(".modal")
    page.select_option("#m-type", "oauth")
    page.wait_for_selector("#m-login:not(.hidden)", timeout=10000)

    assert page.query_selector("#m-fields [data-field='api_key']") is None
    assert page.is_hidden("#m-save"), "there is nothing to save until the login returns"
    assert page.is_visible("#m-start-login")
    # Unconfigured by default, and it must say so rather than fail on the press.
    page.wait_for_selector("#m-login .note", timeout=10000)
    panel = page.inner_text("#m-login")
    assert "oauth.client_id" in panel
    assert page.query_selector("#m-start-login").is_disabled()

    page.click("#m-cancel")
    page.wait_for_selector(".modal", state="detached")


def test_switching_back_to_an_api_key_restores_the_credential_form(page):
    page.click(".nav button[data-tab='accounts']")
    page.click("#acct-add")
    page.wait_for_selector(".modal")
    page.select_option("#m-type", "oauth")
    page.wait_for_selector("#m-login:not(.hidden)")
    page.select_option("#m-type", "anthropic_api")
    page.wait_for_selector("#m-fields [data-field='api_key']")
    assert page.is_visible("#m-save")
    assert page.is_hidden("#m-start-login")
    page.click("#m-cancel")
    page.wait_for_selector(".modal", state="detached")


def test_the_landing_page_is_what_an_unauthenticated_visitor_gets(page, live):
    """A fresh context, because the shared page is already signed in.

    A second sync_playwright() would nest inside the fixture's and raise, so this
    borrows that browser and opens a clean context — which also gets clean
    localStorage, and therefore the landing page rather than a restored session.
    """
    context = page.context.browser.new_context(viewport={"width": 1280, "height": 900})
    visitor = context.new_page()
    errors = []
    visitor.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        visitor.goto(live + "/console", wait_until="networkidle")
        visitor.wait_for_selector("#gate:not(.hidden)", timeout=10000)

        # Lowercased: the eyebrow is uppercased by CSS, so inner_text shouts it.
        text = visitor.inner_text("#gate").lower()
        assert "pooling gateway for claude accounts" in text
        assert "sign in" in text
        # The address it is about to connect to, filled in at boot.
        assert visitor.inner_text("#lp-addr").strip()
        assert live.split("//")[1] in visitor.inner_text("#lp-base")
        # A drawn mark in the header and the favicon, not an emoji.
        assert visitor.query_selector(".lp-top .mark")
        assert "127835" not in visitor.get_attribute("link[rel=icon]", "href")

        # And it is still the way in.
        visitor.fill("#gate-key", "bir_test")
        visitor.click("#gate-go")
        visitor.wait_for_selector("#app:not(.hidden)", timeout=15000)
        visitor.wait_for_selector("#accounts-wrap tr[data-account]", timeout=15000)
        assert visitor.is_hidden("#gate")
        assert errors == [], errors
    finally:
        context.close()


def test_a_rejected_key_returns_you_to_the_landing_page_with_a_reason(page):
    page.evaluate("signOut('That key was rejected.')")
    page.wait_for_selector("#gate:not(.hidden)")
    assert "rejected" in page.inner_text("#gate-msg")
    # Sign back in so the module-scoped page is usable by later tests. Signing in
    # does not reset the open tab, so say which one we want.
    page.fill("#gate-key", "bir_test")
    page.click("#gate-go")
    page.wait_for_selector("#app:not(.hidden)", timeout=15000)
    page.click(".nav button[data-tab='pool']")
    page.wait_for_selector("#stream [data-request]", timeout=15000)


def test_a_gateway_that_stops_answering_is_said_so_not_hidden(page, live):
    """The console used to keep polling a dead gateway forever, four endpoints every
    five seconds, while the live chip read "polling" and stale numbers sat there
    looking current. The browser console filled with hundreds of failures and the
    screen said nothing useful.
    """
    context = page.context.browser.new_context(viewport={"width": 1280, "height": 900})
    visitor = context.new_page()
    errors = []
    visitor.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        visitor.goto(live + "/console", wait_until="networkidle")
        visitor.fill("#gate-key", "bir_test")
        visitor.click("#gate-go")
        visitor.wait_for_selector("#accounts-wrap tr[data-account]", timeout=15000)

        context.set_offline(True)
        visitor.wait_for_selector("#banner .note.err", timeout=20000)

        text = visitor.inner_text("#banner")
        assert "Cannot reach the gateway" in text
        assert live.split("//")[1] in text, "it names the address that is not answering"
        assert "offline" == visitor.inner_text("#live-text"), "not 'polling'"
        assert "stale" in visitor.get_attribute(".main", "class"), \
            "numbers from before the outage must not read as current"

        # And it backs off rather than hammering.
        visitor.wait_for_function(
            "state.pollDelay > 5000", timeout=20000)
        assert visitor.evaluate("state.pollDelay") <= 30000, "the backoff is capped"

        context.set_offline(False)
        visitor.click("#retry-now")
        visitor.wait_for_function(
            "!document.querySelector('#banner .note.err')", timeout=20000)
        assert visitor.evaluate("state.pollDelay") == 5000, "backoff resets on recovery"
        assert "stale" not in (visitor.get_attribute(".main", "class") or "")
        # The stream is reconnected rather than left on its own long backoff.
        visitor.wait_for_function(
            "document.getElementById('live-text').textContent === 'live'", timeout=20000)
        assert errors == [], errors
    finally:
        context.set_offline(False)
        context.close()


def test_a_rejected_key_costs_one_request_not_five(page, live):
    """signIn used to start the poll and the event stream before knowing the key was
    any good, so a wrong key produced 401s on status, horizon, requests and events
    before the screen said anything. The stream then retried the refused key on its
    own timer.
    """
    context = page.context.browser.new_context(viewport={"width": 1280, "height": 900})
    visitor = context.new_page()
    refused = []
    visitor.on("response",
               lambda r: refused.append(r.url) if r.status in (401, 403) else None)
    try:
        visitor.goto(live + "/console", wait_until="networkidle")
        # The boot probe is one 401 by design: it is how an open gateway is detected.
        assert len(refused) == 1, refused
        refused.clear()

        visitor.fill("#gate-key", "bir_not_a_real_key_000000")
        visitor.click("#gate-go")
        visitor.wait_for_selector("#gate-msg .note.err", timeout=15000)
        visitor.wait_for_timeout(4000)   # long enough for a retry loop to show itself

        assert len(refused) == 1, f"one rejected request, got {len(refused)}: {refused}"
        message = visitor.inner_text("#gate-msg")
        assert "rejected" in message
        assert "TOKENBIRYANI_KEY" in message, "say where the right key lives"
        assert not visitor.is_hidden("#gate"), "and stay on the landing page"

        # The real key still works from the same screen.
        refused.clear()
        visitor.fill("#gate-key", "bir_test")
        visitor.click("#gate-go")
        visitor.wait_for_selector("#accounts-wrap tr[data-account]", timeout=15000)
        assert refused == []
    finally:
        context.close()
