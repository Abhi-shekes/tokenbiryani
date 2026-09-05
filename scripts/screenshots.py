#!/usr/bin/env python3
"""Capture the console screenshots that `docs/` links to.

Run against a gateway you are happy to photograph — these images go into the
repository, and whatever the console is showing goes with them: account names, file
paths, spend. Point it at a demo pool rather than production if that matters.

    python scripts/screenshots.py --url http://127.0.0.1:8787 --key bir_...

Signs in with a console ticket rather than a pasted key, so no admin key is ever in
an address bar or a screenshot. Fails loudly on any page error: an image of a broken
console is worse than no image.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import httpx
from playwright.sync_api import sync_playwright

#: Long enough for the screen transition to finish. A shot taken mid-fade shows a
#: washed-out panel and reads as a rendering bug.
SETTLE_MS = 900


def capture(url: str, key: str, out: pathlib.Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    ticket = httpx.post(
        url + "/admin/console-ticket", headers={"x-api-key": key}, timeout=10.0
    ).json()["ticket"]

    errors: list = []
    with sync_playwright() as play:
        browser = play.chromium.launch()
        page = browser.new_page(viewport={"width": 1340, "height": 900})
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        def shot(name: str) -> None:
            page.wait_for_timeout(SETTLE_MS)
            page.screenshot(path=str(out / name))

        page.goto(f"{url}/console?t={ticket}", wait_until="domcontentloaded")
        page.wait_for_selector("#app:not(.hidden)", timeout=15000)
        page.wait_for_selector("#accounts-wrap tr[data-account]", timeout=15000)
        shot("console-overview.png")

        page.click(".nav button[data-tab='accounts']")
        page.wait_for_selector("#accounts-table tr[data-row]")
        shot("console-accounts.png")

        # The subscription card: three token sources, and the machine scan behind
        # the first of them.
        page.click("#acct-add")
        page.wait_for_selector(".modal")
        page.select_option("#m-type", "oauth")
        page.wait_for_selector("#m-cred-path", timeout=10000)
        shot("console-subscription-session.png")
        page.click("[data-mode='token']")
        page.wait_for_selector("#m-login [data-field='api_key']")
        shot("console-subscription-token.png")
        page.click("#m-cancel")
        page.wait_for_selector(".modal", state="detached")

        # A config account turned off from the console: stored here, not written to
        # the file, and revertible. Taken before any account detail is opened — that
        # panel does not refresh on a toggle, so it would sit under the row saying
        # the opposite of it.
        first = page.get_attribute("#accounts-table tr[data-row]", "data-row")
        if page.query_selector(f"tr[data-row='{first}'] [data-revert]") is None:
            page.click(f"tr[data-row='{first}'] [data-toggle]")
            page.wait_for_selector(f"tr[data-row='{first}'] [data-revert]", timeout=10000)
            shot("console-config-override.png")
            page.once("dialog", lambda dialog: dialog.accept())
            page.click(f"tr[data-row='{first}'] [data-revert]")
            page.wait_for_function(
                f"!document.querySelector(\"tr[data-row='{first}'] [data-revert]\")",
                timeout=10000,
            )

        page.click(f"[data-row='{first}'] [data-open]")
        page.wait_for_selector("#account-detail .card")
        shot("console-account-detail.png")

        page.click(".nav button[data-tab='settings']")
        page.wait_for_selector("#set-strategy", timeout=10000)
        shot("console-settings.png")

        page.click(".nav button[data-tab='keys']")
        page.wait_for_selector("#keys tbody tr")
        shot("console-keys.png")

        # The editor needs a key it is allowed to edit — config keys belong to the
        # file. Mint one if the pool has none, and take it away again afterwards, so
        # running this leaves the gateway as it found it.
        borrowed = page.query_selector("[data-edit-key]") is None
        if borrowed:
            page.fill("#k-name", "docs-demo")
            page.fill("#k-pool", first)
            page.fill("#k-rpm", "60")
            page.click("#k-create")
            page.wait_for_selector("[data-edit-key='docs-demo']", timeout=10000)

        page.click("[data-edit-key]")
        page.wait_for_selector("#k-pool-list")
        shot("console-key-editor.png")
        page.click("#k-cancel")
        page.wait_for_selector(".modal", state="detached")

        if borrowed:
            page.once("dialog", lambda dialog: dialog.accept())
            page.click("[data-revoke='docs-demo']")
            page.wait_for_function(
                "!document.querySelector(\"[data-edit-key='docs-demo']\")", timeout=10000
            )

        browser.close()

    if errors:
        print("page errors, refusing to ship these images:", file=sys.stderr)
        for error in errors:
            print("  " + error, file=sys.stderr)
        return 1
    for path in sorted(out.glob("console-*.png")):
        print(f"  {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--key", required=True, help="an admin virtual key")
    parser.add_argument(
        "--out",
        default=str(pathlib.Path(__file__).resolve().parent.parent / "docs" / "images"),
    )
    args = parser.parse_args()
    return capture(args.url.rstrip("/"), args.key, pathlib.Path(args.out))


if __name__ == "__main__":
    raise SystemExit(main())
