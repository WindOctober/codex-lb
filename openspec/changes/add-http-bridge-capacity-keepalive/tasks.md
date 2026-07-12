# Tasks

- [x] 1. Add HTTP bridge keepalive payload helpers and request-state wait metadata.
- [x] 2. Emit keepalive SSE while waiting for session creation/capacity.
- [x] 3. Emit keepalive SSE while waiting for HTTP bridge upstream events.
- [x] 4. Add targeted regression tests and run validation.
- [x] 5. Emit keepalives before `response.created` even when initial HTTP error propagation is enabled.
- [x] 6. Preserve immediate HTTP errors and encode errors arriving after a keepalive as terminal SSE events.
- [x] 7. Verify a current Codex CLI remains on one request across more than 300 seconds of upstream silence.
