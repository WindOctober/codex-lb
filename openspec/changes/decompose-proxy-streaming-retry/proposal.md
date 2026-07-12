## Why

Ordinary HTTP streaming account selection, continuity ownership, transient same-account retry, cross-account failover, request-budget handling, upstream event parsing, and settlement remain interleaved in two methods totaling about one thousand lines inside `ProxyService`. This keeps the primary non-bridge request path coupled to the service monolith and obscures the boundary between orchestration and upstream event handling.

## What Changes

- Introduce a typed ordinary-streaming implementation boundary.
- Move retry/failover orchestration and one-attempt SSE processing outside `ProxyService`.
- Move streaming-only timeout, event suppression, and latency helpers to the same canonical module.
- Retain explicit adapters for settings and the monkeypatch-compatible upstream stream factory.
- Ratchet the proxy service architecture limit after extraction.

## Impact

- Affected code: proxy service, ordinary streaming implementation, architecture tests, focused proxy tests, and streaming integration tests.
- No API, model, routing, affinity, account-selection, retry-count, timeout, continuity, SSE, usage-settlement, or error-contract changes.
