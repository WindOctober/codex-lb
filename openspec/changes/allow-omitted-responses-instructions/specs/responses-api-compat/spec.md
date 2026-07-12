## ADDED Requirements

### Requirement: Responses instructions omission compatibility
The service MUST accept OpenAI-compatible Responses and Responses Compact request payloads that omit `instructions`. The service MUST normalize an omitted `instructions` value to an empty string before forwarding while continuing to require `input`.

#### Scenario: Codex Responses Lite omits instructions
- **WHEN** a Codex client sends a Responses request with a model and input but no `instructions` field
- **THEN** request validation succeeds
- **AND** the forwarded payload contains an empty-string `instructions` value

#### Scenario: Compact request omits instructions
- **WHEN** a client sends a Responses Compact request with a model and input but no `instructions` field
- **THEN** request validation succeeds
- **AND** the forwarded payload contains an empty-string `instructions` value
