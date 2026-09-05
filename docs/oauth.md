# Claude subscription accounts

The console can sign in to a Claude subscription (Max or Pro) and pool it alongside
API-key accounts. This page covers what that costs you, the three values you have to
supply before it works, and how the flow behaves.

Read [ADR-0004](adr/0004-subscription-login-in-core.md) for why it is built this way.

---

## Before you turn it on

### The terms question

Anthropic's consumer terms restrict sharing subscription accounts and using them
outside the provided interfaces. Pooling several subscriptions so their limits add up
is what that is aimed at. The practical risks are yours: account suspension, and for a
public deployment, a takedown request.

Routing **your own single** subscription through **your own local** gateway is a
different thing from pooling several to multiply limits. This gateway cannot tell the
difference and does not try to. You know which you are doing.

### It costs you most of the routing

Every feature that distinguishes this project from a generic proxy is computed from
`anthropic-ratelimit-*` response headers, and subscription sessions do not send them.

| | API key account | Subscription account |
|---|---|---|
| Failover across accounts | yes | yes |
| Prompt-cache affinity | yes | yes |
| Headroom-aware routing | yes | **no** |
| Leases that actually bind | yes | **no** — nothing to reserve against |
| Admission control (fail fast) | yes | **no** |
| Capacity horizon | yes | **no** |
| Usage and cost charts | yes | yes — these come from the response body |
| Behaviour when saturated | wait for a known reset | reactive 429 backoff |

The console shows these accounts with empty meters and a "no limits" tag. That is the
honest rendering, not a bug.

### The middle path worth considering first

Mixed pools work. Put the subscription in at `cost_tier: 0`, your API keys above it,
and run `cost_tiered`: the router drains the subscription first and spills to API keys
when it is exhausted. Each credential is used the way it is licensed, and the paid
capacity still gets real headroom-aware routing.

---

## Configuration

Login is **disabled until you configure it**, and the console tells you exactly which
values are missing rather than failing on the button press.

```yaml
oauth:
  client_id: ""             # required
  authorize_url: ""         # required
  token_url: ""             # required
  redirect_uri: ""          # blank = paste the code by hand (see below)
  scopes: []
  refresh_skew_seconds: 300     # renew this far ahead of expiry
  refresh_interval_seconds: 60  # how often sessions are checked
```

### Why these are blank

Anthropic does not publish the OAuth endpoints its first-party clients use, and this
project has never been run against a real subscription session. A hard-coded guess
would look like a working feature and fail in a way no error message could explain, so
the gateway refuses to guess.

To fill them in, read them off the client you already trust — the authorize URL your
own Claude login flow opens carries the `client_id`, and the token endpoint is the one
that URL's documentation or your client's network log names. If a value turns out to
be wrong you get a plain OAuth error (`invalid_client`, `invalid_grant`) in the
console, not a mystery.

### Redirect or paste

- **Blank `redirect_uri` (default).** The provider shows an authorization code; you
  paste it into the console. This works without registering a callback anywhere, and
  it is the safer default.
- **A configured `redirect_uri`.** Point it at `http://127.0.0.1:8787/admin/oauth/callback`.
  That page displays the code for you to copy — it deliberately does **not** complete
  the login itself, because the browser the provider redirects carries no admin key.

---

## Adding an account

1. **Accounts → Add account**, choose **Claude subscription (Max / Pro)**.
2. Give it an ID and a name. The name is what you will see everywhere.
3. **Log in with Claude** opens a tab. Authorize the gateway.
4. Paste the code back and finish. The account joins the pool immediately.

Repeat per subscription. Each gets its own session.

## How sessions are kept alive

The gateway renews each session `refresh_skew_seconds` before it expires, in a
background task. This is what makes pooling more than one subscription possible:
reading a credentials file the Claude CLI keeps fresh covers a single session, and a
gateway holding several has no CLI to lean on.

If a renewal fails, the account is **disabled with a reason** rather than retried
forever. An account that reads "ready" and returns 401 on every request is the outcome
worth avoiding. Log in again to replace the session.

Access and refresh tokens are encrypted at rest with the same key as API credentials
(`store.secret_key_path`, or `TOKENBIRYANI_SECRET_KEY`). Neither ever appears in an
admin API response — both are stripped by name.

## What is still unverified

The same thing that was unverified before: the provider's real endpoints, and the
`anthropic-beta` value a subscription session expects (`options.beta_header`, default
`oauth-2025-04-20`). The PKCE flow itself is RFC 7636 and is tested against a scripted
token endpoint. See `TODO.md`.
