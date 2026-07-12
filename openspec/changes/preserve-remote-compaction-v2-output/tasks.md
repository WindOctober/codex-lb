## Implementation

- [x] Add OpenSpec requirement for `remote_compaction_v2` context compaction item passthrough.
- [x] Preserve `context_compaction` output items only when the downstream request advertises `remote_compaction_v2`.
- [x] Add focused regression coverage for streamed and collected Responses normalization.
- [x] Run focused tests and lint.
- [x] Validate on isolated codex-lb stack before restarting the primary backend.
