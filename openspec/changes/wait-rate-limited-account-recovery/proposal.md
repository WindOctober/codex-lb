# Wait For Recoverable Account Rate Limits

## Why

When every eligible account is temporarily rate-limited or cooling down with a known recovery time, codex-lb currently reports no available accounts to some HTTP bridge paths. That terminates active Codex links even though the condition is recoverable.

## What Changes

- Carry structured retry-after metadata through account selection when all eligible accounts are temporarily rate-limited, quota-limited, or cooling down.
- Keep HTTP bridge session creation and reconnect paths waiting within the existing request budget when account recovery is expected.
- Reuse existing HTTP bridge keepalive events while waiting so downstream clients do not see a silent stream.

## Non-Goals

- No infinite waits for permanently unavailable accounts.
- No retry of an upstream request after it may already have reached OpenAI.
- No Caddy/gateway or database schema changes.
