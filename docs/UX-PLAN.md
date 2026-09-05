# M10 — Onboarding and account configuration

**Status: implemented.** All five stages below are built and tested; §11 records what
each one became. The findings in §1 are kept in the past tense they were written in,
because they are the argument for the changes rather than a list of open work.

Companion to the project plan in `PLAN.md` and to [UI-DESIGN.md](UI-DESIGN.md). Those two describe
a product that is built. This one describes the path a new user actually walks to reach
it, which was broken in three places before they saw a single screen.

Everything in §1 was reproduced against `f8ff61c` in a clean directory with an empty
environment. Line references are to that commit.

---

## 1. The walk

The README's first three lines are the whole promise:

```bash
pip install tokenbiryani
tokenbiryani init                 # writes tokenbiryani.yaml + a virtual key
tokenbiryani serve                # then add your accounts at /console
```

Run them:

```
$ tokenbiryani init
wrote tokenbiryani.yaml
$ tokenbiryani serve
config error: config references ${ANTHROPIC_API_KEY} but that environment variable is not set
```

The gateway does not start. That is finding F1, and the rest of the walk is blocked
behind it. Below is everything the walk hits once each blocker is removed by hand.

### The findings, in the order a user meets them

| | Finding | Where |
|---|---|---|
| **F1** | `init` then `serve` fails on a clean machine | `cli.py:169-179`, `config.py:25-31` |
| **F2** | The console's add-account path needs an extra that a default install does not have | `pyproject.toml:29`, `secrets.py:33-41` |
| **F3** | The admin key is a copy-paste out of terminal scrollback | `cli.py:213`, `cli.py:331-342` |
| **F4** | The wizard and the Connect screen hand over `<your key>`, a placeholder | `console.html:1745`, `console.html:1679` |
| **F5** | The wizard has a three-dot stepper and two steps; step 2 is a 12-field modal | `console.html:1724`, `console.html:1301` |
| **F6** | An account ID is required, has no default, and is a validation error | `console.html:1419`, `gateway.py:346-354` |
| **F7** | Cost is `—` for everyone out of the box; no price table ships | `config.py:178-181` |
| **F8** | `doctor` closes the project's largest open risk and nothing tells anyone to run it | `TODO.md`, `cli.py:386` |
| **F9** | "Log in with Claude" is offered in the dropdown before it is possible | `console.html:369-377` |

**F1 — `init` writes a config `serve` cannot load.** `init`'s template declares
`acct-01` with `api_key: ${ANTHROPIC_API_KEY}`; `interpolate()` raises `ConfigError` on
an unset variable rather than treating it as absent; `serve` prints and exits 1. This
contradicts the comment in the same template ("Or add accounts from the console — no
editing this file") and the README line directly beside it. `serve` was already taught
that an empty pool is a legitimate first run (`cli.py:305-312`); `init` was not taught
to write one.

**F2 — the primary path needs `pip install 'tokenbiryani[secrets]'`.** `cryptography`
is an optional extra. Accounts added through the console are encrypted before they
reach the store, so on a default install the first save returns
`503 cannot store credentials: storing account credentials needs the cryptography
package`. The error is well-written and correctly classified — the problem is that the
one path the README, the landing page and the wizard all point at is the one path a
default install cannot finish.

**F3 — the key exists only in scrollback.** `init` prints `bir_…` once. `serve` prints
it masked, deliberately and correctly. The console gate then asks for it and, on
failure, tells the user to go read a YAML file. There is no command that opens the
console signed in.

**F4 — the product will not tell you your own key.** `finishOnboarding()` and
`renderGuide()` both emit `export ANTHROPIC_AUTH_TOKEN=<your key>` while the browser is
holding a working key in `localStorage`, and the Keys screen shows every key masked.
The user is one copy button away from being done and is instead sent back to the file.

**F5/F6 — the add-account modal is an admin form doing an onboarding job.** Twelve
inputs — type, name, ID, API key, base URL, cost tier, priority, spend cap, models — of
which two matter on a first run. The ID has no default, so the most likely first
interaction with the form is the error `An account needs an ID.` for not having
invented `acct-02`. The name field is directly beside it.

**F7 — question 2 cannot be answered on a default install.** UI-DESIGN §1 names three
questions the console exists to answer; the second is "why is my bill what it is". No
`pricing:` block ships, so the Usage cost chart, the spend column, account spend caps
and per-key caps are all empty until the operator hand-transcribes a price table.

**F8 — the most valuable command is undiscoverable.** TODO.md: "One real API key and
one run of that command closes this item. It is still the most important one." Nothing
in `serve`'s output, the wizard, or the console mentions `doctor` exists.

**F9 — a dead end is offered as a choice.** Selecting "Claude subscription (Max / Pro)"
explains that three config values are missing and disables the button. The honesty is
right (ADR-0004); presenting it as a selectable option first is not.

---

## 2. The principle this plan is enforcing

**The console is the configuration surface. The YAML is for people who want a file.**

The product already says this — the landing page says it, the template comment says it,
the wizard exists for it. The code does the opposite at every one of the three points
above. Nothing in this plan is a new idea; it is making the build match its own stated
design.

Two corollaries, and they decide the ordering:

- Anything required before the first screen is a defect, not a feature request. F1, F2
  and F3 are Stage 1 for that reason alone.
- A form that asks for something the product could work out is asking the user to do
  the product's job. F6 is the clearest case; F5 is the same thing at scale.

---

## 3. Stage 1 — make the first five minutes work

Blocking. Nothing else in this plan is worth building before these land.

### 1.1 `init` writes a config that starts

`cli.py` `TEMPLATE`: comment the `accounts:` block out entirely and point at the
console. Add `tokenbiryani init --api-key sk-ant-…` to write a live account inline for
operators who want the file, and have bare `init` mention the flag.

```yaml
accounts: []
  # Add accounts from the console at http://127.0.0.1:8787/console — stored
  # encrypted, renameable, testable and rotatable in place. Or declare them here:
  #
  # - id: acct-01
  #   type: anthropic_api
  #   api_key: ${ANTHROPIC_API_KEY}
```

Separately, `interpolate()` should keep raising on a missing variable — that behaviour
is correct and load-bearing for real deployments. The fix is not to weaken it; it is to
stop `init` from writing a reference nobody asked for.

*Acceptance:* `init && serve` in an empty directory with an empty environment starts and
serves the wizard. New test in `tests/test_cli.py` that runs both under `env -u
ANTHROPIC_API_KEY`.

### 1.2 `cryptography` becomes a base dependency

`pyproject.toml`: move `cryptography>=41.0` into `dependencies`. Keep `secrets = []` as
an empty extra so existing `pip install 'tokenbiryani[secrets]'` lines keep working.

This is the smallest change in the plan and removes the hardest wall. Storing
credentials is no longer an add-on; it is the main path.

*Acceptance:* a venv built from base dependencies only can add an account through the
console. Assert it in CI with a base-deps job, not just by inspection.

### 1.3 `tokenbiryani console` — open the browser already signed in

A new command that reads the admin key from the config, hands it to the running gateway
in exchange for a single-use ticket, and opens the browser on it.

```
POST /admin/console-ticket   (authenticated with the admin key)
  → { "ticket": "…", "expires_in": 60 }

POST /admin/console-session  (keyless; the ticket is the credential)
  { "ticket": "…" } → { "key": "bir_…" }        one redemption, then gone
```

The gate keeps its rule — **a key never travels in a URL**. A ticket does: it is
loopback-only, dies after one redemption or 60 seconds, and the page calls
`history.replaceState` to drop it the moment it is exchanged. Refuse to mint one when
the gateway is bound off-loopback.

`serve`'s output gains one line:

```
  console  http://127.0.0.1:8787/console   ·  tokenbiryani console  opens it signed in
```

*Acceptance:* `tokenbiryani console` on a fresh install lands on the wizard with no
typing. A replayed ticket returns 401. Tests in `tests/test_console.py`.

### 1.4 The empty-pool message stops being a warning

`cli.py:308-312` prints the empty pool to stderr, which reads as a fault. It is the
expected first run. Print it to stdout as the next step:

```
  no accounts yet — add your first at http://127.0.0.1:8787/console
```

**Stage 1 total:** roughly one day. It converts a broken quickstart into a working one.

---

## 4. Stage 2 — make adding an account one decision

### 2.1 Essentials, then Advanced

Rebuild `accountModal()` around what a first run needs:

| Above the fold | Folded into a collapsed `Advanced` |
|---|---|
| Type · Name · Credential | ID, base URL, cost tier, priority, spend cap, models |

Nothing is removed — the admin fields stay one click away, and they stay expanded by
default when the modal opens in edit mode, because that is when they are the point.

### 2.2 Derive the ID from the name

Slugify the name, uniquify against the current snapshot, show the result as the
Advanced ID field's value so it is visible and editable rather than magic. `Work
account` → `work-account`. `_SAFE_ID` in `gateway.py:69` already constrains the shape;
the slugifier targets it directly.

F6 disappears: there is no longer a required field with no default.

### 2.3 Test before storing, not after

Today "Add & test" writes the account, then tests it — so a typo'd key becomes a
`disabled` row the operator has to find and clean up. Invert it.

Add `POST /admin/accounts/test` taking an unsaved credential payload with no ID, reusing
`Gateway.test_account`'s `/v1/models` probe against a throwaway upstream. The modal's
primary button becomes **Test and add**: probe, show the result inline, store only on
success, with an explicit "add it anyway" for the case where the operator knows better.

*Acceptance:* a wrong key never produces a stored account. Test in
`tests/test_managed_accounts.py`.

### 2.4 Recognise a pasted credential

`sk-ant-` selects Anthropic API. An ARN-shaped or `AKIA`-shaped paste selects Bedrock; a
service-account JSON blob selects Vertex. Cheap, and it removes the first dropdown from
the first run entirely.

**Stage 2 total:** two to three days, mostly `console.html` plus one endpoint.

---

## 5. Stage 3 — close the loop after the account is in

### 3.1 The wizard gets its missing step

The stepper promises three steps and delivers two. The missing one is the most valuable
screen in the product, and it already exists as a CLI command.

- **Step 2 — Verify.** Runs the credential probe *and* the rate-limit-header check that
  `doctor` performs, and reports it plainly: every header present, or exactly which are
  missing and what it costs ("those windows stay empty, so this account reads as full
  and routing degrades to round-robin"). This is F8 answered without asking anyone to
  remember a command name.
- **Step 3 — Connect.** Real values, not placeholders (see 3.2).

### 3.2 Hand over a key that works

Both the wizard's last step and the Connect screen get a key selector at the top that
substitutes into every snippet on the page:

- **the key you signed in with** — already in `localStorage`, works on `/v1/messages`;
- **mint a new one** — a non-admin `claude-code` key created inline via the existing
  `POST /admin/keys`, shown once, which is the right default to *recommend*: a client
  should not be carrying an admin key.

Copy button on the whole block. F4 closes.

### 3.3 One implementation of the header check

Extract the expected-header list and the parse-and-report logic out of `cmd_doctor`
into `core/diagnostics.py`, and have both the CLI and the new
`POST /admin/accounts/{id}/diagnose` call it. Two copies of that header list is exactly
the drift the command exists to prevent.

**Stage 3 total:** two to three days. This is the stage that makes the product feel
finished rather than merely working.

---

## 6. Stage 4 — make the numbers mean something

**This stage needs a decision from you before any of it is built.** TODO.md lists "No
price list" under *Not built, and deliberately*, and `config.py:178-181` states the
reason: guessing prices in code means silently billing against stale numbers. That
principle is sound and I am not proposing to discard it.

But its current cost is that a default install cannot answer question 2 of the three the
console was designed around, and that every cost surface — the Usage chart, the spend
column, account caps, key caps — renders `—` for a user who has done nothing wrong.

Three ways out, in the order I'd recommend them:

| Option | What ships | Cost |
|---|---|---|
| **A. Dated data file** (recommended) | `prices.yaml` inside the package, carrying an explicit `as_of` date. `pricing: builtin` in config opts in; `init` writes it. The console shows "prices as of 2026-03-01 — check Anthropic's page" beside every cost. | A stale table is possible, but it is *dated and labelled*, which is the difference between a wrong number and an unattributed one. Prices are data, not code — the original principle holds. |
| **B. `tokenbiryani prices`** | A command that writes a `pricing:` block into the user's YAML, from a bundled table or a fetch. | Still a manual step, but a one-command one. Keeps the file the single source of truth. |
| **C. Status quo** | Nothing. | Costs stay `—` by default; the Usage screen's headline capability is opt-in. |

Whichever is chosen, the empty state must teach rather than draw nothing: the Usage
screen with no pricing should name the missing config block and link to Settings, not
render a blank chart.

**Stage 4 total:** one day for A or B, plus the empty states.

---

## 7. Stage 5 — operator ergonomics

Not blocking, but this audience lives in a terminal and the console is currently the
only way to reach a managed account.

- `tokenbiryani accounts add|list|test|rm` against the same store the console writes.
- **Test all** on the Accounts screen — the natural action after restoring a backup or
  rotating keys, currently N clicks.
- A `disabled` account gets a fix-it path in place: its Edit modal opens focused on the
  credential field with the last error stated above it, and Save re-enables. The
  clearing logic already exists at `gateway.py:396-400`; nothing surfaces it.
- F9: move the "not configured" note for subscription login *above* the type dropdown,
  so the constraint is visible before the choice rather than after it.

---

## 8. Not in this plan, deliberately

- **No SPA framework, no build step.** UI-DESIGN §8. Every UI item above is reachable in
  the existing hand-written HTML and CSS, and the console must stay inside a `pipx
  install`.
- **No writable config over HTTP.** TODO.md's standing exclusion. Accounts and keys are
  credentials, not configuration, and stay the exception; nothing here proposes editing
  `tokenbiryani.yaml` from the browser.
- **No guessed OAuth endpoints.** ADR-0004. Stage 5 makes the unconfigured state legible
  earlier; it does not invent values.
- **No weakening of `interpolate()`.** Failing loudly on a missing variable is right for
  a real deployment. F1 is `init`'s bug, not interpolation's.

---

## 9. Sequencing

| Stage | Items | Effort | Unblocks |
|---|---|---|---|
| **1** | 1.1–1.4 | ~1 day | The quickstart in the README |
| **2** | 2.1–2.4 | 2–3 days | Adding an account without reading docs |
| **3** | 3.1–3.3 | 2–3 days | Trusting the pool; `doctor`'s value, by default |
| **4** | pending a decision | ~1 day | Question 2 on a default install |
| **5** | ergonomics | 2 days | Day-two operation |

Stages 1 and 2 are the ones with outsized return; everything after them is polish on a
product that already works.

## 10. Acceptance checklist

The plan is done when all of these pass on a clean machine, from a plain
`pip install tokenbiryani`:

- [ ] `tokenbiryani init && tokenbiryani serve` starts, with no environment set.
- [ ] `tokenbiryani console` opens a signed-in browser on the wizard.
- [ ] An account can be added, tested and verified without touching a file or a doc.
- [ ] A bad credential is rejected before it is stored.
- [ ] The last screen hands over a working key, not `<your key>`.
- [ ] The rate-limit-header check has run and reported, without anyone naming `doctor`.
- [ ] Cost is a number, or the empty state says exactly which block is missing.


---

## 11. What was built

Every stage landed. The parts that turned out differently from the plan are called out.

### Stage 1 — the first five minutes

- **`init` writes a config that starts.** The template declares `accounts: []` with the
  console named in a comment; `tokenbiryani init --api-key sk-ant-…` seeds one inline
  for operators who want the file. `interpolate()` is untouched — failing loudly on a
  missing variable is right, and F1 was `init`'s bug.
- **`cryptography` moved into `dependencies`**, with `secrets = []` kept as an empty
  extra so existing install lines still resolve. `secrets.py`'s error message no longer
  names an extra that no longer exists, and the Dockerfile drops it.
- **`tokenbiryani console`** opens a signed-in browser. `core/handoff.py` holds the
  ticket book — one redemption, 60 seconds, loopback only — behind
  `POST /admin/console-ticket` (admin-authenticated) and `POST /admin/console-session`
  (keyless; the ticket *is* the credential). The page redeems it and calls
  `history.replaceState` before anything else runs. A key still never travels in a URL.
- **The empty pool is a next step on stdout**, not a warning on stderr.

### Stage 2 — one decision to add an account

- Essentials above the fold, admin fields inside a collapsed `Advanced` — open by
  default when editing, because that is when they are the point.
- The ID is derived from the name, uniquified against the pool, and **stops following
  the moment the operator types one of their own**. That last part was not in the plan
  and matters: a field that overwrites what you typed into it is worse than an empty one.
- **Test before store.** `POST /admin/accounts/test` probes an unsaved credential
  through `Gateway.probe_credential`; the primary button is now *Test and add*, and
  *Add without testing* is the deliberate override. `test_account` was refactored onto
  the same `_probe` so there is one implementation.
- Pasted credentials select their own type (`sk-ant-`, `AKIA`/`ASIA`/`arn:aws:`, a
  service-account JSON blob).

### Stage 3 — closing the loop

- **The wizard's missing step exists**, and so does step 3. Both were unreachable:
  `refresh()` cleared `state.onboardStep` the instant the pool stopped being empty,
  which is exactly when steps 2 and 3 were supposed to run. The wizard now holds the
  screen until it hands over.
- **Step 2 verifies**: `POST /admin/accounts/{id}/diagnose` runs the credential probe
  *and* the rate-limit-header check. **This one nearly shipped wrong**, and the end-to-end
  run on a clean install is what caught it — see §12.
- **Step 3 hands over a real key** — the one this browser signed in with, or a freshly
  minted non-admin client key, which is what it recommends. The Connect screen uses the
  same component, so `<your key>` is gone from both.
- `core/diagnostics.py` owns the expected-header list. `cmd_doctor` reads it too, so
  the two callers cannot drift.

### Stage 4 — prices

Built as **Option A**, the recommended one. `src/tokenbiryani/prices.yaml` is a dated
table (`as_of`, `source`) opted into with `pricing: builtin`; a mapping may set
`builtin: true` alongside its own entries, which override it model by model. The
snapshot carries `pricing.{models,as_of,source}`, Settings shows it, and the Usage
screen's cost chart is replaced by the config block to add rather than drawn empty.

The standing "no price list" rule is intact in the form that mattered: prices are
dated data whose date is on screen, not a dict compiled into a release.

### Stage 5 — ergonomics

- `tokenbiryani accounts list | add | test | rm`. `add` probes before storing, exactly
  as the console does, with `--no-test` as the override; `test` with no argument tests
  every account and reports the header check per account.
- **Test all** on the Accounts screen, with a failure summary.
- A disabled managed account gets a **Fix** button that opens its editor focused on the
  credential with the reason it stopped stated above the field.
- Subscription login is labelled *— not configured* in the dropdown itself, so the
  constraint is visible before the choice instead of after it.

### Coverage

`pytest` = 341 passed, 1 skipped, up from 296. New tests cover the clean-machine
`init && serve` path, ticket mint/redeem/replay/off-loopback refusal, the
unsaved-credential probe, diagnose across all three of its answers, the builtin price
table and its override, and eight browser tests for the modal, the derived ID, the
paste detection, the rejection-before-store, the Connect screen's real key, the
unpriced cost chart and the ticket landing.

`tests/test_onboarding_ui.py` is the one that matters most: an empty pool, a real
browser, and the whole first run driven start to finish — the flow whose middle two
steps were unreachable and which no unit test could have caught.


---

## 12. The bug the plan would have shipped

Stage 3 said the verify step should run `doctor`'s header check "without the spend" by
riding on the `/v1/models` probe. Every unit test agreed, because the in-process mock
answers both endpoints from one handler.

Running it against a clean install and the standalone mock server said otherwise:

```
! work    credential ok, 9 rate-limit header(s) missing
      anthropic-ratelimit-requests-limit
      anthropic-ratelimit-requests-remaining
      ... all nine
```

**`GET /v1/models` carries no `anthropic-ratelimit-*` headers.** It never did. So the
check as designed would have told every new user, at the end of their first
successful setup, that all nine headers were missing and their routing had degraded to
round-robin — about a pool that was working perfectly.

That is worse than not checking at all. A check that cries wolf on the happy path
teaches people to ignore it, and this is the one check the project most needs run.

### The fix

There are **three** answers, not two, and the third is the honest one for a new account:

| `limits_source` | Meaning |
|---|---|
| `checked` | Headers from a real `/v1/messages` response — one this account already served, or one the caller paid for. |
| `not_observed` | No completion has gone through this account yet. Nothing is wrong; there is nothing to check. |
| `unobservable` | This upstream reports no limit headers by design (a subscription session). |

`AccountRuntime.observe_headers` now keeps the `anthropic-ratelimit-*` headers from the
last real response, so the free check has something true to read as soon as any traffic
flows. When there is none, the wizard says so and offers **Run the check now** —
`{"spend": true}`, one `max_tokens=1` completion, priced in the copy as a fraction of a
cent. `tokenbiryani accounts test --deep` is the same trade from the terminal. Nothing
spends money unasked.

### Two smaller things it also caught

- The standalone mock's `/v1/models` accepted **any** key, so the first end-to-end run
  reported a deliberately wrong credential as accepted. `testing/server.py` now
  authenticates it — and still returns no limit headers, deliberately, because a mock
  that emitted them there would hide exactly the bug above.
- `accounts --url` only parsed *before* the subcommand, so `accounts add x --url …`
  failed with an argparse error. The flags moved onto a shared parent parser.

The lesson is narrow and worth keeping: **the in-process mock and the real socket path
are not the same test.** Two of these three were invisible to 336 passing unit tests
and took one run of the actual quickstart to find.
