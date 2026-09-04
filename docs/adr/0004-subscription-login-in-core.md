# 4. Subscription login moves into core, disabled until configured

**Status:** accepted — supersedes the "no OAuth adapter in the core distribution"
clause of [ADR-0003](0003-api-keys-only.md). Everything else in 0003 still stands.

## Context

ADR-0003 kept subscription support out of core and, later, allowed it as a separate
distribution at `contrib/tokenbiryani-oauth/`. That adapter reads the credentials
file the Claude CLI maintains — `~/.claude/.credentials.json` — and deliberately
implements no refresh, because the CLI already refreshes that file.

That design has a ceiling: **one file means one subscription per machine.** Pooling
is the reason this project exists, and you cannot pool a thing there is only one of.
Adding a second subscription means a second credentials file, produced by logging in
somewhere else with a different CLI profile, and kept fresh by something. In practice
that is not a workflow anyone completes.

Users asked for the flow every comparable tool has: press a button, authorize in a
browser, repeat per account.

The two objections in ADR-0003 have not gone away, and are not answered here:

1. **The terms question is unchanged.** Pooling subscription accounts to exceed
   per-account limits runs against Anthropic's consumer terms. Routing your own single
   subscription through your own local gateway is a different act from pooling several
   to multiply limits, and this code cannot tell them apart.
2. **It still routes badly.** Subscription sessions send no `anthropic-ratelimit-*`
   headers, so headroom routing, binding leases, admission control and the capacity
   horizon all go dark for these accounts. Failover and prompt-cache affinity remain.

## Decision

Move the account type into core as `providers/oauth.py`, add the OAuth 2.0 + PKCE
login to `core/oauth.py`, and expose it in the console — **shipped inert.**

`oauth.client_id`, `oauth.authorize_url` and `oauth.token_url` default to empty, and
the login refuses with an error naming exactly which are missing.

That last part is the substance of this decision, so it is worth being blunt about
why. Anthropic does not publish the OAuth endpoints its first-party clients use, and
this project has never been run against a real subscription session. Hard-coding a
guess would produce a feature that *looks* supported, fails in a way no error message
explains, and quietly asserts something the authors do not know. Requiring three
config values keeps the flow correct and complete while leaving the one unverifiable
part where it belongs: with the operator who can check it.

Three further consequences of moving it in, rather than leaving it in contrib:

- **The gateway owns the token lifecycle.** It stores access and refresh tokens
  encrypted per account, and renews them ahead of expiry — the thing contrib
  deliberately does not do, and cannot, because it has no login of its own.
- **`observable_limits` is forced false**, not merely warned about. Left true, such an
  account reads as permanently full, wins every routing comparison against accounts
  reporting an honest partial budget, absorbs the pool's traffic and then starts
  429ing. That is a routing hazard, not a preference.
- **A failed refresh disables the account** with a reason, rather than retrying
  forever. An account that reads "ready" and 401s every request is the worse outcome.

**`contrib/tokenbiryani-oauth` is deprecated, and had to be.** A plugin may not shadow
a built-in account type — `register_upstream` refuses, by an older and still correct
rule — so once `oauth` became built in, that package's entry point could no longer
load. Leaving it would have meant a warning on every startup for anyone who had
installed it.

Rather than break those users, core absorbed the package's token sources verbatim:
`options.credentials_path`, `options.token_env` and `options.access_token` mean exactly
what they meant, and an account naming one keeps reading its token from there instead
of using a gateway-held session. Explicit configuration wins, so an upgrade cannot
silently move where an account's credential comes from. The package remains as a
re-export shim that warns on import and can be uninstalled at any time.

## Consequences

- The supported, tested-against-a-real-API path is still API keys. Nothing about the
  recommendation in ADR-0003 changes.
- Every configuration `contrib/tokenbiryani-oauth` supported keeps working against core
  alone, and is tested both in core's suite and in the shim's.
- `cryptography` matters more than it did: a refresh token is a longer-lived secret
  than the access token it mints. Both are encrypted at rest and both are stripped by
  name from every payload the admin API returns.
- The mixed-pool middle path is still the one worth recommending first — subscription
  at `cost_tier: 0` under `cost_tiered`, API keys above it — so each credential is
  used the way it is licensed and the paid capacity keeps real headroom routing.
- One more unverified surface joins the list in `TODO.md`. The login flow is plain
  RFC 7636 and is tested against a scripted token endpoint; what remains unproven is
  the same thing that was unproven before — the provider's actual endpoints, and the
  `anthropic-beta` value a subscription session expects.
