## Why

After responsibility extraction, the proxy facade still carries private wrappers, aliases, and a constant with no production or test callers, while HTTP bridge session acquisition initializes an unused logger and compact transport compatibility delegates through a redundant single-caller wrapper. Removing only symbols proven unreferenced shrinks accidental surface and duplication without touching active routing, continuity, or transport contracts.

## What Changes

- Remove the unused service-level `_resolve_prompt_cache_key` wrapper, `_account_supports_http_bridge_request_model` wrapper, previous-response matcher alias, and `_TEXT_DELTA_EVENT_TYPES` constant together with imports used only by them.
- Remove the unused `logging` import and logger initialization from HTTP bridge session acquisition.
- Fold the single-caller `_call_core_compact_responses` passthrough into the existing `_core_compact_responses_compatible` method while preserving service-module core transport replacement and optional-keyword filtering.
- Keep all canonical affinity, model-support, WebSocket event-matching, streaming, and HTTP bridge implementations unchanged.
- Keep every architecture- or OpenSpec-protected facade name unchanged, including response-create diagnostic helpers.
- Add absent-symbol architecture ratchets and rerun focused behavior/static/isolated validation.

## Capabilities

### New Capabilities

- `proxy-facade-maintenance`: Defines evidence and behavior-preservation constraints for pruning proven-dead proxy facade symbols and redundant passthrough layers.

### Modified Capabilities

None.

## Impact

- Affected code: `app/modules/proxy/service.py`, `app/modules/proxy/_service/http_bridge/session_acquire.py`, proxy architecture tests, and focused affinity/bridge/WebSocket/compact regressions.
- No public API, payload, account routing, continuity, concurrency, streaming, WebSocket, persistence, configuration, deployment, or runtime behavior changes.
- No new dependency is introduced.
