# Fail Closed Provider Compact

## Why

Remote compaction expects `/responses/compact` to return the upstream compact
context-window contract unchanged. The local provider `responses` wire API path
was converting compact requests into normal streamed Responses calls and then
synthesizing a compact-shaped response, which can break clients that parse
compact output as the next canonical context window.

## What Changes

- Stop using streamed Responses as a surrogate implementation for compact on
  non-Codex upstream provider wire APIs.
- Return an explicit not-implemented compact error when the selected upstream
  provider does not support the Codex compact contract.
- Keep provider-unsupported compact failures account-neutral so account health
  is not penalized for a static provider capability mismatch.

## Non-Goals

- Implement compact for OpenAI-compatible provider APIs that do not expose a
  compact endpoint.
- Change direct Codex `/codex/responses/compact` pass-through behavior.
