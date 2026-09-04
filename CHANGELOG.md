# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Anthropic-compatible gateway: `POST /v1/messages` (streaming and not),
  `count_tokens` and `models` passthrough.
- Multi-account pooling with a live `anthropic-ratelimit-*` mirror, token estimation,
  and atomic leases.
- Six routing strategies; `sticky_headroom` is the default.
- Error taxonomy separating account action from request action.
- Transparent streaming failover up to the first content delta.
- Session affinity with prefix fingerprinting, and cache-break accounting.
- Admission control and a bounded priority wait queue.
- Virtual keys with model, pool, rpm and spend scoping.
- Prometheus metrics, structured logs, admin API with a per-request routing inspector.
- `tokenbiryani` CLI: `init`, `serve`, `status`, `keygen`.
- Scriptable mock Anthropic upstream, usable in-process or as a real server.
