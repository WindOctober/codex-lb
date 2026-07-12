## Why

Streaming, compact, transcription, downstream WebSocket, and HTTP bridge runtimes all share the same account credential-freshness lifecycle, but provider bypass, timeout scoping, repository access, refresh admission, and compatibility dispatch still live on `ProxyService`. Extracting that security-sensitive lifecycle gives it one typed owner and also restores the service-level `RefreshError` export accidentally lost during earlier decomposition.

## What Changes

- Add a dedicated account-freshness runtime mixin with explicit repository and refresh-admission capabilities.
- Move `_ensure_fresh` and `_ensure_fresh_with_budget` out of `service.py` without changing provider bypass, AuthManager singleflight, timeout ContextVar, force-refresh, or admission behavior.
- Preserve dynamic dispatch through `self._ensure_fresh` so existing instance/class monkeypatches and older fakes without `timeout_seconds` remain compatible.
- Preserve the `AuthManager` and `ACCOUNT_PROVIDER_API_KEY` service facade exports and restore the previously available `RefreshError` export used by HTTP bridge integrations.
- Add architecture ownership ratchets, direct timeout/compatibility tests, and focused regressions across all consuming transports.
- Leave account error classification, health mutation, routing, selection, and failover in their current owners.

## Capabilities

### New Capabilities

- `proxy-account-freshness-runtime`: Defines the shared typed lifecycle for provider-aware account credential freshness and refresh-budget scoping.

### Modified Capabilities

None.

## Impact

- Affected code: `app/modules/proxy/service.py`, a new module under `app/modules/proxy/_service/`, proxy architecture tests, and focused account-freshness/transport tests.
- Internal service facade compatibility for `RefreshError` is restored to its pre-decomposition state.
- Public APIs, account routing and switching, refresh protocol semantics, continuity, concurrency limits, streaming/WebSocket behavior, persistence schema, deployment, and runtime configuration remain unchanged.
- No new dependency is introduced.
