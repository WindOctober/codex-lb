# Proxy Account Freshness Runtime Context

## Purpose and Scope

Ordinary streaming, compact, transcription, downstream WebSocket connection, HTTP bridge session creation, and HTTP bridge reconnect all need the same account credential-freshness lifecycle. The shared runtime bypasses OAuth work for API-key providers, scopes token-refresh timeouts, opens the proxy repository bundle, and delegates freshness to canonical `AuthManager` with shared refresh admission.

This capability does not define refresh intervals, OAuth token exchange, persistence, singleflight keys, failure cooldown, deactivation, account health, routing, selection, or failover.

## Decisions and Constraints

- Both `_ensure_fresh` and `_ensure_fresh_with_budget` live together because the latter is the compatibility entry point for the former across every transport.
- `AuthManager` remains the canonical owner of refresh decisions, singleflight, admission acquisition during a real refresh, token persistence, and permanent-error deactivation.
- API-key providers return before timeout ContextVar, repository, or admission work.
- Timeout overrides are token-scoped and reset in `finally`, so nested caller state survives success, exceptions, and cancellation.
- The budget adapter resolves `self._ensure_fresh` at call time and filters only unsupported optional keywords, preserving instance/class monkeypatches and older fakes.
- `AuthManager`, `ACCOUNT_PROVIDER_API_KEY`, and `RefreshError` are explicit service facade aliases. `RefreshError` restores the repository-HEAD compatibility surface used by HTTP bridge integrations.
- Internal runtime modules must not import the service facade.

## Failure Modes

- Moving provider bypass after repository entry would make API-key accounts depend on irrelevant OAuth infrastructure.
- Failing to reset the ContextVar can leak one request's small refresh timeout into later requests.
- Calling a statically bound freshness method can bypass transport tests and local integrations that replace the inherited method.
- Omitting the refresh-admission callback can allow concurrent real token refreshes to escape the configured local budget.
- Removing the canonical `RefreshError` facade alias breaks integrations that construct the expected refresh failure through `app.modules.proxy.service`.

## Concrete Example

A compact request selects a stale OAuth account with three seconds left in its request budget. The inherited budget method forwards `force=False` and a three-second optional timeout to the shared runtime. The runtime pushes that timeout, enters the repository bundle, constructs `AuthManager` with the account repository and token-refresh admission callback, and delegates freshness. If a concurrent request already refreshes the same token material, AuthManager joins its singleflight. On return or failure, the runtime restores the caller's prior timeout before compact retry logic continues.

## Operational Notes

Validate changes with direct provider, AuthManager, timeout, cancellation, and legacy-signature tests; existing refresh admission/singleflight tests; and focused compact, transcription, streaming, downstream WebSocket, and HTTP bridge consumers. Runtime preflight uses isolated backend/mock ports and must not restart or drain the primary backend or alter Caddy.
