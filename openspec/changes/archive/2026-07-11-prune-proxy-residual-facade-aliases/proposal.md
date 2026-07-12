## Why

The first proven-redundancy cleanup exposed two additional private service re-exports whose canonical budget and rate-limit owners already have all real callers. Keeping zero-reference aliases in the proxy facade expands accidental compatibility surface and contradicts the extracted runtime ownership without preserving any demonstrated behavior.

## What Changes

- Remove the service-level `_request_budget_seconds` and `_usage_window_row_from_entry` re-export imports after a module-aware reference and contract audit.
- Preserve the canonical helpers and all production callers in `_service/budget.py` and `_service/rate_limits.py` unchanged.
- Extend the existing absent-name architecture ratchet to prevent either alias from returning to the facade.
- Rerun canonical budget/rate-limit behavior, static, OpenSpec, and isolated runtime validation.
- Continue deferring API-key policy refresh, owner forwarding, account health, selection, topology, durable continuity, and compatibility-protected aliases because they overlap active changes or retained contracts.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `proxy-facade-maintenance`: Adds the two audited residual re-exports to the absent-name contract and explicitly preserves their canonical budget and rate-limit owners.

## Impact

- Affected code: `app/modules/proxy/service.py`, proxy architecture tests, and the `proxy-facade-maintenance` specification/context.
- No public API, request budget, rate-limit projection, account routing, continuity, concurrency, streaming, WebSocket, persistence, configuration, deployment, or runtime behavior changes.
- No new dependency or abstraction is introduced.
