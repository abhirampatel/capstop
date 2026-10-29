# Security

## Reporting a vulnerability
Please do not open a public issue. Report privately via GitHub's private vulnerability reporting on the repo's
Security tab ("Report a vulnerability").

## What Capstop protects against
- Runaway spend: each run (`X-Run-Id`) has a hard dollar cap, and an operator-set global ceiling applies across all
  runs. Calls that would exceed a cap are refused with HTTP 402 before they reach the provider.
- Costs come from the provider's reported usage priced with a local price table (`capstop/prices.json`).
- API keys are passed through to the provider and are never stored or logged.
- Host header check: every request must have a Host of `127.0.0.1`, `localhost`, `::1` or the `--host` value (port
  ignored). Any other Host gets HTTP 421 `{"error": {"type": "misdirected_request"}}` and is not handled.

## What it does not protect against
- It does not authenticate callers. Anyone who can reach the port can use it and read `/runs`. It is meant for
  local use on 127.0.0.1.
- Pre-flight estimates can under- or over-count; a run can end slightly above its cap.
- It is not a billing system. Its numbers are not checked against your provider invoice, and the price table can
  go stale.

## Platforms
Tested on Linux only. Windows, macOS and the Dockerfile are untested.
