# tokenbiryani

A pooling gateway for Claude accounts. One Anthropic-compatible endpoint in front of
every credential you own.

It routes on live rate-limit headers, keeps a conversation on the account holding its
prompt cache, fails over without dropping a stream, and queues honestly when the whole
pool is dry.

```bash
pip install tokenbiryani
tokenbiryani init
tokenbiryani serve

export ANTHROPIC_BASE_URL=http://localhost:8787
export ANTHROPIC_AUTH_TOKEN=bir_...
claude
```

![The console's Accounts screen: the pool, where each account was declared, and what
it is allowed to spend](images/console-accounts.png)

## Start here

- **[Quickstart](quickstart.md)** — running in about a minute.
- **[Why is my bill higher?](caching.md)** — the one page to read before changing
  the routing strategy. Cache-blind routing costs about 2x.
- **[Streaming and retries](streaming.md)** — what the gateway can hide, and the one
  failure it cannot.

## Design axiom

The gateway is a **router, not a rewriter**. It never edits prompts, tools, or
parameters. That single rule is what keeps it correct across API changes and safe to
sit in a hot path.

The one exception is the Bedrock and Vertex adapters, which must move the model into
the URL. That translation lives in one file and nowhere else — see
[Account types](providers.md).
