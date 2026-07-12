## ADDED Requirements

### Requirement: Durable HTTP bridge trims verified full-resend continuations
When a Responses request reattaches to a durable HTTP bridge session and the client sends a full-resend payload without `previous_response_id`, the service MUST use the durable latest response id as a continuity anchor only if the stored input metadata proves that the request input begins with the already-completed context. When the proof succeeds, the service MUST trim the stored prefix before forwarding upstream. When the proof is absent or mismatched, the service MUST NOT inject the durable anchor for that full-resend payload.

#### Scenario: Trimmable durable full resend
- **GIVEN** a durable HTTP bridge session has a latest response id, latest input item count, and latest full-input fingerprint
- **AND** the incoming list-shaped input starts with the stored prefix
- **WHEN** the request is a full-resend continuation without `previous_response_id`
- **THEN** the service forwards the request with the durable latest response id
- **AND** it trims the already-stored input prefix before forwarding

#### Scenario: Unverifiable durable full resend remains conservative
- **GIVEN** a durable HTTP bridge session has a latest response id
- **AND** the stored input metadata is missing or does not match the incoming input prefix
- **WHEN** the request is a full-resend continuation without `previous_response_id`
- **THEN** the service does not inject the durable latest response id
- **AND** it forwards the original request body without continuity trimming
