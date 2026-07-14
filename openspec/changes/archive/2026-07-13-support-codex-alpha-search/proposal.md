## Why

Current Codex CLI and VS Code builds execute the built-in Web Search tool through `POST /backend-api/codex/alpha/search`. codex-lb has no matching route, so FastAPI returns `405 Method Not Allowed`; production recorded hundreds of these failures and a live `web.run` call reproduces them.

## What Changes

- Add a typed Codex-compatible `POST /backend-api/codex/alpha/search` endpoint.
- Route search requests through an eligible authenticated account while preserving Codex session affinity where available.
- Forward the official search request body and return the upstream JSON response without exposing account credentials.
- Reuse existing account freshness, API-key model restrictions, routing policy, error classification, and account failover behavior.
- Add integration and unit coverage for successful forwarding, validation, affinity, authentication, and upstream failures.

## Capabilities

### New Capabilities

- `codex-search-compat`: Proxy Codex's typed alpha search command through managed ChatGPT accounts.

### Modified Capabilities

None.

## Impact

The change affects the proxy API, a focused JSON upstream client, account-selection runtime, and tests. It does not expose a generic passthrough route, alter Responses streaming semantics, change Caddy, or add persistence.
