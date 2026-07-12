## Why

Audio transcription is an independent request workflow, but its complete selection, freshness, timeout, retry, accounting, and logging orchestration remains embedded in `ProxyService`. This keeps a self-contained endpoint lifecycle coupled to Responses streaming and HTTP bridge internals.

## What Changes

- Introduce a typed transcription runtime mixin with explicit service capabilities.
- Move the transcription request workflow out of `ProxyService` while preserving its public method signature.
- Preserve inbound header filtering, request budgets, account selection, token refresh, one-time 401 retry, timeout overrides, account health updates, error translation, and terminal request logging.
- Add a narrow compatibility wrapper for the upstream transcription client so existing test and diagnostic patch points remain valid.
- Ratchet architecture tests to prevent transcription orchestration from returning to the service facade.

## Capabilities

### New Capabilities

- `proxy-transcription-runtime`: Defines the internal responsibility boundary and behavior-preservation contract for proxied audio transcription orchestration.

### Modified Capabilities

None.

## Impact

- Affected code: proxy service, a focused transcription runtime module, architecture tests, and transcription unit/integration tests.
- No endpoint, request/response schema, model, routing policy, timeout value, account state, database, or public error contract changes.
