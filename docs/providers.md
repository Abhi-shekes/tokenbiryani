# Account types

All types share one pool, so a request can fail over from an API key to Bedrock.

| `type` | Auth | Streaming | Extra |
|---|---|---|---|
| `anthropic_api` | `x-api-key` | native SSE | — |
| `bedrock` | SigV4 | binary event-stream, decoded to SSE | `pip install "tokenbiryani[bedrock]"` |
| `vertex` | bearer token | native SSE | `pip install "tokenbiryani[vertex]"` |

## The one place bodies are rewritten

Bedrock and Vertex serve the same Messages API but address the model in the URL and
stamp their own `anthropic_version`. That is exactly two edits, in
`providers/translate.py`, and nowhere else. Everything else the caller sent —
including parameters this gateway has never heard of — travels through untouched.

## Model mapping

Callers use one model name; each platform has its own ids.

```yaml
accounts:
  - id: acct-bedrock
    type: bedrock
    cost_tier: 1.2
    options:
      region: us-east-1
      profile: default        # omit for the ambient AWS credential chain
      model_map:
        claude-test-1: anthropic.claude-3-5-sonnet-20241022-v2:0

  - id: acct-vertex
    type: vertex
    options:
      project: my-project
      region: us-central1
      model_map:
        claude-test-1: claude-3-5-sonnet-v2@20241022
```

Mapping is exact match, then trailing wildcard, then the name as given — which is
right when the caller already knows the platform's id.

## `base_url` is an override

Leave it unset and each adapter uses its own default. Setting it globally would point
a Bedrock account at `api.anthropic.com`, which is a mistake the type system cannot
catch for you. Set it only for a private endpoint:

```yaml
    base_url: https://vpce-xxxx.bedrock-runtime.us-east-1.vpce.amazonaws.com
```

## Bedrock streaming

Bedrock frames its stream in AWS event-stream binary, not SSE. The adapter decodes
each frame and re-emits the Anthropic event, so the commit boundary and usage
accounting keep reading ordinary streaming. Frames split across TCP chunks are
handled; that case is tested.
