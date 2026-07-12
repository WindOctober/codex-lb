## Why

Codex remote compaction v2 sends a normal streamed `/backend-api/codex/responses`
request with the `remote_compaction_v2` beta feature enabled. The proxy's public
Responses stream normalization was treating the returned `context_compaction`
output item as an unknown public item and dropping it when it had no text field.
The Codex client then saw zero compaction output items and failed the thread
compaction.

## What Changes

- Preserve `context_compaction` output items on Responses streams when the
  downstream request opts into `remote_compaction_v2`.
- Keep the existing public output-item normalization for requests that do not
  opt into that beta feature.
- Add regression coverage for streaming and collected Responses payloads.

## Out of Scope

- Changing `/responses/compact` direct compact behavior.
- Synthesizing compaction items locally.
- Expanding opaque compaction item passthrough for non-beta public Responses
  requests.
