# 3. API keys are the supported credential type

**Status:** accepted

## Context

The obvious use for a pooling gateway is combining Claude Pro/Max subscription accounts.
Two problems:

1. Pooling subscription accounts to exceed per-account limits runs against Anthropic's
   consumer terms. For a public open-source project that is a takedown and reputation
   risk, not a footnote.
2. It routes badly. Subscription sessions expose no `anthropic-ratelimit-*` headers, so
   the header-driven routing this project exists for degrades into reactive 429 backoff —
   which is what every generic proxy already does.

## Decision

Ship `anthropic_api` (API keys) as the supported, documented, tested path. Keep
`providers.base.Upstream` open so other credential types *can* be implemented. No OAuth
adapter in the core distribution.

**Superseded in part.** Subscription support was first added as a separate
distribution, then folded into core; see [ADR-0004](0004-subscription-login-in-core.md)
for why, and for what it still costs you. The decision above is otherwise unchanged:
the supported, tested-against-a-real-API path is API keys.

## Consequences

- The routing differentiators work as designed for every shipped credential type.
- Bedrock and Vertex shipped as adapters against the same interface, which is the
  evidence that the interface was the right seam.
- Anyone who wants subscription pooling can implement one interface; that is their call
  and their risk, not the project's.
