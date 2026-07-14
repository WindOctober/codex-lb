## Why

Transient upstream WebSocket handshake timeouts on the direct Responses path are currently misreported as exhaustion of the full proxy request budget, even when hundreds of seconds remain. The resulting terminal SSE event bypasses safe account failover, and the shared HTTP bridge budget wrapper has the same ambiguity for nested timeout exceptions, causing avoidable Codex reconnects during brief upstream or network instability.

## What Changes

- Distinguish an expired outer request deadline from a shorter nested upstream-connect timeout.
- Preserve the real connect-timeout classification while request budget remains.
- Classify pre-first-event direct handshake timeouts with a dedicated transient SSE code so account failover can run before any downstream event is visible.
- Retry a safe, pre-send HTTP bridge connection failure on another eligible account within the existing attempt and request-budget bounds.
- Keep continuity-bound requests fail-closed when switching accounts would invalidate upstream response state.
- Add regression coverage for nested timeouts, exhausted deadlines, account failover, and terminal cleanup.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `proxy-upstream-websocket-runtime`: Refine WebSocket connection-budget semantics and require safe pre-send HTTP bridge failover for transient connect failures.

## Impact

Affected code is limited to the direct Responses WebSocket client, shared upstream WebSocket budget wrapper, HTTP bridge session creation, their tests, and the corresponding runtime specification. Public endpoint shapes and configured timeout values do not change.
