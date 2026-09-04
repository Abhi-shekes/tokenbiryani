# Security policy

This project holds live API credentials in memory and proxies prompt traffic. Please
treat findings accordingly.

## Reporting

Report privately via GitHub's **Report a vulnerability** button on the Security tab, or
email the maintainers. Please do not open a public issue for anything exploitable.
Expect an acknowledgement within 72 hours.

## Design commitments

- Upstream credentials never appear in logs, error messages, or admin responses. Keys are
  masked, and keys too short to mask safely are hidden entirely.
- Virtual keys are compared in constant time.
- Prompt bodies are not logged unless `observability.log_bodies` is explicitly enabled.
- The server binds to loopback and refuses a public interface without both
  `server.allow_remote: true` and at least one configured key.

## Out of scope

- Running the gateway on an untrusted network with `allow_remote: true` and no keys.
- Anything requiring an attacker to already have read access to `tokenbiryani.yaml`,
  which by design contains real credentials.
