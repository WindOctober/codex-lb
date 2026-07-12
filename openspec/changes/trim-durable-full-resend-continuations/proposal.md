# trim-durable-full-resend-continuations

## Why
Codex clients can resend the full conversation input after bridge owner loss, process restart, or a missing live local session. The current durable bridge only stores the latest turn state and response id, so the proxy cannot prove that a large full-resend payload starts with the already-stored context. To avoid duplicating context, it conservatively avoids durable anchor injection for full-resend payloads, causing large cold upstream requests and poor cache behavior.

## What Changes
- Persist latest completed input count and full-input fingerprint in durable HTTP bridge session metadata.
- Allow durable previous-response anchor injection for full-resend payloads only when the current input prefix matches the persisted metadata.
- Trim the already-stored prefix before forwarding the anchored request upstream.
- Preserve the current conservative behavior when persisted metadata is absent or mismatched.

## Impact
- Affected code: `app/db/models.py`, durable bridge repository/coordinator, HTTP bridge streaming logic, Alembic migrations, and bridge tests.
- Affected APIs: Responses HTTP bridge paths used by Codex and `/v1/responses`.
- Operational impact: large resumed Codex turns can avoid re-sending already-stored context after durable bridge recovery.
