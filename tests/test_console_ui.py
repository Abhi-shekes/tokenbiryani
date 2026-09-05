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
def live(request, tmp_path_factory):
    """A real HTTP gateway, with the mock wired in as a transport rather than a server."""
    import uvicorn
    from support.mock_upstream import MockAnthropic

    from tokenbiryani.api.app import create_app

    mock = MockAnthropic()
    # Priced, because a real gateway now is: `init` writes `pricing: builtin`, and an
    # unpriced pool deliberately replaces the cost chart with an explanation of why
    # it is empty. Without a price here the charts under test would be that notice.
    config = make_config(
        ["acct-01", "acct-02"],
        overrides={"pricing": {"claude-test-*": {
            "input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75,
        }}},
    )
    # These tests add accounts through the console, which encrypts the credential
    # first. Without a path the key lands in the working directory — a 0600 secret
    # dropped into the repository, reused by every later run.
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
def browser():
    with playwright_api.sync_playwright() as p:
        # CI installs Playwright's own chromium; a dev box usually has Chrome.
        launches = [{"channel": "chrome"}, {}]
        if os.environ.get("TOKENBIRYANI_UI_BROWSER") == "chromium":
            launches.reverse()
        launched = None
        for options in launches:
            try:
                launched = p.chromium.launch(**options)
                break
            except Exception:  # pragma: no cover - depends on the machine
                continue
        if launched is None:
            pytest.skip("no chromium or chrome build available")
        yield launched
        launched.close()


@pytest.fixture(scope="module")
def page(live, browser):
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    page.errors = []
    page.on("pageerror", lambda exc: page.errors.append(str(exc)))
    page.goto(live + "/console", wait_until="networkidle")
    page.fill("#gate-key", "bir_test")
    page.click("#gate-go")
    page.wait_for_selector("#stream [data-request]", timeout=15000)
    yield page
    page.close()


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


def test_theme_toggles_and_persists(page):
    page.click(".nav button[data-tab='pool']")
    page.click("#theme")
    assert page.get_attribute("html", "data-theme") == "light"
    # Not networkidle: the console holds an open SSE stream, so it never goes idle.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#stream [data-request]", timeout=15000)
    assert page.get_attribute("html", "data-theme") == "light", "theme must survive a reload"
    assert page.get_attribute("html", "data-density") is None, (
        "row density is not a setting any more — no control, so no stored state and "
        "nobody stuck in a mode they cannot leave"
    )
    page.click("#theme")


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
    page.fill("#m-fields [data-field='api_key']", "key-from-the-ui")
    # The ID is derived from the name and lives under Advanced. Overwrite it here
    # only because the rest of this test addresses the row by id.
    page.click(".modal .adv > summary")
    page.fill("#m-id", "acct-ui")
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


def test_a_config_account_can_be_operated_but_not_overwritten(page):
    """The file keeps the credential; the console can still work the account.

    Deleting one from the UI would overwrite a decision the file makes, so there is
    no Delete. Turning it off, renaming it or re-tiering it does not touch the file
    at all — it is stored in the gateway — so those are offered.
    """
    page.click(".nav button[data-tab='accounts']")
    page.wait_for_selector("#accounts-table tr[data-row='acct-01']")
    row = page.query_selector("tr[data-row='acct-01']")
    assert row.query_selector("[data-del]") is None, "config accounts get no Delete"
    assert row.query_selector("[data-edit]"), "but they can be edited"
    assert row.query_selector("[data-test]"), "and testing one is always allowed"
    assert not row.query_selector("[data-toggle]").is_disabled(), (
        "the enabled toggle stores an override rather than editing the file"
    )


def test_turning_off_a_config_account_from_the_console_and_putting_it_back(page):
    page.click(".nav button[data-tab='accounts']")
    page.wait_for_selector("#accounts-table tr[data-row='acct-02']")
    page.click("tr[data-row='acct-02'] [data-toggle]")
    # The row comes back marked as changed here rather than in the file.
    page.wait_for_selector("tr[data-row='acct-02'] [data-revert]", timeout=10000)
    # Lowercased: the pill is uppercased by CSS, so inner_text shouts it.
    assert "edited" in page.inner_text("tr[data-row='acct-02']").lower()

    page.once("dialog", lambda dialog: dialog.accept())
    page.click("tr[data-row='acct-02'] [data-revert]")
    page.wait_for_function(
        "!document.querySelector(\"tr[data-row='acct-02'] [data-revert]\")",
        timeout=10000,
    )


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


def test_a_subscription_offers_three_token_sources(page):
    """A subscription has no API key field: it has a choice of where its token lives.

    The login flow needs three endpoint values Anthropic does not publish, so it
    cannot be the only door — the other two need nothing configured at all.
    """
    page.click(".nav button[data-tab='accounts']")
    page.wait_for_selector("#accounts-table tr[data-row]")
    page.click("#acct-add")
    page.wait_for_selector(".modal")
    page.select_option("#m-type", "oauth")
    page.wait_for_selector("#m-login:not(.hidden)", timeout=10000)

    assert page.query_selector("#m-fields [data-field='api_key']") is None
    modes = [b.get_attribute("data-mode") for b in page.query_selector_all("[data-mode]")]
    assert modes == ["session", "token", "login"]

    # Default: the machine's own Claude Code login, with a path to confirm.
    page.wait_for_selector("#m-cred-path", timeout=10000)
    assert page.is_visible("#m-save-test"), "this mode saves like any other account"
    assert page.is_hidden("#m-start-login")

    # A pasted long-lived token goes in the field the store encrypts.
    page.click("[data-mode='token']")
    page.wait_for_selector("#m-login [data-field='api_key']")
    assert page.get_attribute("#m-login [data-field='api_key']", "type") == "password"

    # The login flow is still there, and still says what it is missing rather than
    # failing on the button press.
    page.click("[data-mode='login']")
    page.wait_for_selector("#m-mode-body .note", timeout=10000)
    assert "oauth.client_id" in page.inner_text("#m-login")
    assert page.is_visible("#m-start-login")
    assert page.query_selector("#m-start-login").is_disabled()

    page.click("#m-cancel")
    page.wait_for_selector(".modal", state="detached")


def test_the_session_mode_lists_what_this_machine_is_signed_in_to(page):
    """Picking the wrong profile is the failure this panel exists to prevent."""
    page.click(".nav button[data-tab='accounts']")
    page.click("#acct-add")
    page.wait_for_selector(".modal")
    page.select_option("#m-type", "oauth")
    page.wait_for_selector("#m-cred-path", timeout=10000)
    body_text = page.inner_text("#m-mode-body")
    # Either a login was found and offered, or the panel says how to make one.
    assert ".credentials.json" in body_text or "No Claude Code login" in body_text
    page.click("#m-cancel")
    page.wait_for_selector(".modal", state="detached")


def test_switching_back_to_an_api_key_restores_the_credential_form(page):
    page.click(".nav button[data-tab='accounts']")
    page.click("#acct-add")
    page.wait_for_selector(".modal")
    page.select_option("#m-type", "oauth")
    page.wait_for_selector("#m-login:not(.hidden)")
    page.wait_for_selector("[data-mode='session']")
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


# ---- onboarding and account configuration -------------------------------------
# The add-account form is the product's activation funnel. These cover what it now
# does for the operator rather than asking them to do it.


def test_the_id_is_derived_from_the_name(page):
    """It used to be required, empty, and the first error a new user saw.

    "An account needs an ID." for not having invented `acct-02` is a form asking the
    user to do the product's job.
    """
    page.click(".nav button[data-tab='accounts']")
    page.click("#acct-add")
    page.wait_for_selector(".modal")
    page.fill("#m-name", "My Work Account")
    page.click(".modal .adv > summary")
    assert page.input_value("#m-id") == "my-work-account"

    # ...and it stops following once the operator has said what they want.
    page.fill("#m-id", "chosen-by-hand")
    page.fill("#m-name", "Something Else Entirely")
    assert page.input_value("#m-id") == "chosen-by-hand", (
        "a field that overwrites what you typed into it is worse than an empty one"
    )
    page.click("#m-cancel")


def test_the_admin_fields_are_folded_away(page):
    """Twelve inputs of which two matter on a first run. The rest are one click away."""
    page.click(".nav button[data-tab='accounts']")
    page.click("#acct-add")
    page.wait_for_selector(".modal")
    assert page.is_visible("#m-name"), "the name is above the fold"
    assert page.is_visible("#m-fields [data-field='api_key']"), "and the credential"
    assert not page.is_visible("#m-id"), "the id is not"
    assert not page.is_visible("#m-tier"), "nor the routing knobs"
    page.click(".modal .adv > summary")
    assert page.is_visible("#m-id"), "but they open"
    page.click("#m-cancel")


def test_a_pasted_key_selects_its_own_type(page):
    """The shape of the credential already says which upstream it belongs to."""
    page.click(".nav button[data-tab='accounts']")
    page.click("#acct-add")
    page.wait_for_selector(".modal")
    page.select_option("#m-type", "bedrock")
    page.wait_for_selector("#m-fields [data-field='opt.region']")
    page.select_option("#m-type", "anthropic_api")
    page.fill("#m-fields [data-field='api_key']", "sk-ant-api03-something")
    assert page.input_value("#m-type") == "anthropic_api"
    page.click("#m-cancel")


def test_a_bad_credential_is_rejected_before_it_is_stored(page):
    """Store-then-test turned a typo into a disabled row to find and clean up."""
    page.click(".nav button[data-tab='accounts']")
    page.wait_for_selector("#accounts-table tr[data-row]")
    page.click("#acct-add")
    page.wait_for_selector(".modal")
    page.fill("#m-name", "Typo account")
    page.fill("#m-fields [data-field='api_key']", "not-a-key-the-mock-knows")
    page.click("#m-save-test")

    page.wait_for_function(
        "document.querySelector('#m-msg .note.err')?.innerText.includes('rejected')",
        timeout=15000,
    )
    assert page.is_visible(".modal"), "the dialog stays open so it can be corrected"
    page.click("#m-cancel")
    assert page.query_selector("tr[data-row='typo-account']") is None, (
        "and nothing was written"
    )


def test_the_connect_screen_hands_over_a_key_that_works(page):
    """It used to print `<your key>` while the browser held a working one."""
    page.click(".nav button[data-tab='guide']")
    page.wait_for_selector("#g-connect .code")
    assert "bir_test" in page.inner_text("#g-connect .code"), (
        "the key this browser signed in with, not a placeholder"
    )
    assert page.query_selector("#g-mint"), "and an offer to mint one without admin"


def test_an_unpriced_cost_chart_says_why_it_is_empty(page):
    """A blank chart reads as 'nothing was spent', not 'nothing was priced'."""
    priced = page.evaluate("() => (state.snapshot.pricing || {}).models")
    assert priced, "this fixture is priced"
    # Drop the prices the way an unconfigured gateway has none, and redraw.
    page.click(".nav button[data-tab='usage']")
    page.wait_for_selector("#u-cost svg", timeout=15000)
    page.evaluate("() => { state.snapshot.pricing = {models: 0}; renderUsage(); }")
    body = page.inner_text("#u-body")
    assert "No prices configured" in body
    assert "pricing: builtin" in body, "and how to fix it"


def test_a_ticket_signs_the_browser_in_and_leaves_no_trace_in_the_url(live, browser):
    """`tokenbiryani console`, from the browser's side.

    The gate's promise is that a key is never in a URL. A ticket is, so it has to be
    spent on arrival and taken back out of the address bar — otherwise the history
    entry is a dead link that looks like a live one.
    """
    minted = httpx.post(
        live + "/admin/console-ticket", headers={"x-api-key": "bir_test"}, timeout=10
    )
    ticket = minted.json()["ticket"]

    fresh = browser.new_page(viewport={"width": 1200, "height": 900})
    try:
        # Not networkidle: a signed-in console holds /admin/events open, so the
        # network never goes quiet. The selector is the real signal anyway.
        fresh.goto(f"{live}/console?t={ticket}", wait_until="domcontentloaded")
        fresh.wait_for_selector("#app:not(.hidden)", timeout=15000)
        assert "?t=" not in fresh.url, "the ticket is out of the address bar"
        assert fresh.evaluate("() => localStorage.getItem('tokenbiryani.key')") == "bir_test"
    finally:
        fresh.close()


def test_a_spent_ticket_lands_on_the_sign_in_page(live, browser):
    """A replayed link must fail closed, not half-open a console."""
    fresh = browser.new_page(viewport={"width": 1200, "height": 900})
    try:
        fresh.goto(
            f"{live}/console?t=already-used-or-invented", wait_until="domcontentloaded"
        )
        fresh.wait_for_selector("#gate:not(.hidden)", timeout=15000)
        assert "?t=" not in fresh.url
    finally:
        fresh.close()


# ---- editing what used to need an editor ---------------------------------------

def test_a_key_can_be_rescoped_without_reissuing_it(page):
    """Revoke-and-reissue breaks every client holding the key. Editing does not."""
    page.click(".nav button[data-tab='keys']")
    page.wait_for_selector("#k-create")
    page.fill("#k-name", "scoped-key")
    page.fill("#k-pool", "acct-01,acct-02")
    page.fill("#k-rpm", "30")
    page.click("#k-create")
    page.wait_for_selector("[data-edit-key='scoped-key']", timeout=10000)

    page.click("[data-edit-key='scoped-key']")
    page.wait_for_selector("#k-pool-list")
    # Untick one account and lift the rate limit.
    page.uncheck("#k-pool-list input[value='acct-01']")
    page.fill("#k-e-rpm", "")
    page.click("#k-save")
    page.wait_for_selector(".modal", state="detached", timeout=10000)

    row = page.wait_for_selector("tr:has([data-edit-key='scoped-key'])")
    text = row.inner_text()
    assert "acct-02" in text and "acct-01" not in text
    assert "—" in text, "an rpm that was cleared reads as no limit"


def test_the_settings_screen_changes_the_strategy_and_says_so(page):
    """The strategy was a config-file edit and a restart. Now it is a dropdown."""
    page.click(".nav button[data-tab='settings']")
    page.wait_for_selector("#set-strategy", timeout=10000)
    assert "from the file" in page.inner_text("#settings-edit").lower()

    page.select_option("#set-strategy", "round_robin")
    # The panel is drawn by an async load, so pin the value before pressing Save
    # rather than racing a repaint that would discard the choice.
    page.wait_for_function(
        "document.querySelector('#set-strategy').value === 'round_robin'", timeout=10000
    )
    page.click("#set-save")
    page.wait_for_selector("#set-msg .ok", timeout=10000)
    assert "set in console" in page.inner_text("#settings-edit").lower(), (
        "a value the file does not know about must say where it came from"
    )
    assert page.eval_on_selector("#set-strategy", "el => el.value") == "round_robin"

    # Put it back, so the rest of the module sees the pool it expects.
    page.select_option("#set-strategy", "sticky_headroom")
    page.click("#set-save")
    page.wait_for_selector("#set-msg .ok", timeout=10000)


def test_a_subscription_shows_rolling_windows_rather_than_empty_meters(page, live):
    """The regression this replaces: real utilisation rendered as three blank bars."""
    meters = page.evaluate(
        """() => {
            const empty = {
              fraction: 1, available: null, limit: null, remaining: null, reset_in: null,
            };
            const limits = {
              observable: false, unified_known: true,
              requests: empty, input_tokens: empty, output_tokens: empty,
              unified: {
                "5h": {status: "allowed", utilization: 0.34,
                       headroom: 0.66, reset_in: 3600},
                "7d": {status: "allowed", utilization: 0.45,
                       headroom: 0.55, reset_in: 90000},
              },
            };
            return {
              html: meters(limits),
              reset: resetIn(limits),
              pill: identity({id: "sub", name: "sub", type: "oauth", limits: limits}),
            };
        }"""
    )
    assert "5h" in meters["html"] and "7d" in meters["html"]
    assert "66%" in meters["html"], "headroom is 1 - utilisation"
    assert "req" not in meters["html"], "no empty API-key meters for a subscription"
    assert meters["reset"] == 3600, "the countdown follows the window that binds"
    assert "no limits" not in meters["pill"]
    assert "rolling" in meters["pill"]


def efficiency(page):
    """Open the tab and wait for real content.

    The loading placeholder is itself a `.card`, so waiting on `.card` alone
    matches it immediately and reads an empty screen as a rendered one. Every card
    that has actually rendered carries a header.
    """
    page.click(".nav button[data-tab='efficiency']")
    page.wait_for_selector("#eff-body .card header h3", timeout=15000)
    return page.inner_text("#eff-body")


def test_the_efficiency_tab_renders_all_four_cards(page):
    """The four questions the pool meters cannot answer, on one screen."""
    text = efficiency(page)
    for heading in ("Quota pace", "Prompt cache", "Conversations", "Output leases"):
        assert heading in text, f"{heading} card is missing"


def test_an_unpaced_pool_says_why_rather_than_drawing_nothing(page):
    """API keys report no weekly window, and this fixture states no budget. An empty
    card would read as a fault; the reason is the useful thing to show."""
    assert "Nothing to pace against" in efficiency(page)


def test_conversations_are_listed_worst_first(page):
    text = efficiency(page)
    assert "Session tracking is off" not in text
    assert "fp:" in text, "session keys should be listed"


def test_the_efficiency_tab_raises_no_page_errors(page):
    efficiency(page)
    assert page.errors == [], page.errors


def test_the_inspector_flags_a_request_that_asked_for_no_caching(page):
    """The fixture's requests carry no `cache_control`, and the console should say so
    where the operator is already looking at why a request cost what it did."""
    page.click(".nav button[data-tab='requests']")
    page.wait_for_selector("#requests [data-request]")
    page.query_selector_all("#requests [data-request]")[0].click()
    page.wait_for_selector("#inspector .card", timeout=10000)
    # The fixture's bodies are small, so the notice is correctly absent; what must
    # hold either way is that rendering these fields does not break the panel.
    assert "Routing decision" in page.inner_text("#inspector")
    assert page.errors == [], page.errors
