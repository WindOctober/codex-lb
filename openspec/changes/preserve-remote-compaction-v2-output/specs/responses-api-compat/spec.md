## MODIFIED Requirements

### Requirement: Responses stream output item normalization preserves beta compaction state

When a downstream `/backend-api/codex/responses` or `/v1/responses` request
includes `remote_compaction_v2` in `X-Codex-Beta-Features`, the service MUST
preserve streamed `context_compaction` output items without rewriting, dropping,
or text-normalizing them.

#### Scenario: Remote compaction v2 output item passes through streaming normalization

- **GIVEN** a downstream Responses request includes `X-Codex-Beta-Features: remote_compaction_v2`
- **WHEN** upstream emits a `response.output_item.done` event whose item has `type: "context_compaction"` and `encrypted_content`
- **THEN** the service forwards that output item unchanged
- **AND** if a non-streaming Responses collector reconstructs the terminal response output from output-item events, it includes that item unchanged
- **AND** if the terminal response output contains an opaque placeholder at the same output index, the service preserves the streamed `context_compaction` item instead of failing the stream as an unsupported output item
- **AND** for `/backend-api/codex/responses` streams, the service MUST preserve Codex-native streamed output items instead of applying public OpenAI SDK output-item contract cleanup

#### Scenario: Non-beta unknown output item behavior remains unchanged

- **GIVEN** a downstream Responses request does not include `remote_compaction_v2`
- **WHEN** upstream emits an unknown output item type
- **THEN** the service applies the existing public Responses output-item normalization rules
