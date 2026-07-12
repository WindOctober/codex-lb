# Keep HTTP Bridge Streams Alive During Capacity Waits

## Why

Codex clients may reconnect when the HTTP bridge stays silent while codex-lb is waiting for session/account capacity or reconnect recovery. The backend should periodically emit lightweight SSE keepalive events while it is still actively working on the request.

## What Changes

- Emit `codex.keepalive` SSE events during long HTTP bridge session creation waits.
- Emit `codex.keepalive` SSE events while an active HTTP bridge request is waiting for upstream events.
- Preserve existing terminal error and timeout behavior.

## Non-Goals

- No Caddy/gateway change.
- No database migration.
- No client-side setting change.
