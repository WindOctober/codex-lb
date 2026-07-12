## Context

Six proxy execution paths—ordinary streaming, compact, transcription, downstream WebSocket connection, HTTP bridge session creation, and HTTP bridge reconnect—depend on the same two `ProxyService` freshness methods. Those methods bypass OAuth refresh for API-key providers, scope the core refresh timeout with a ContextVar, open the proxy repository bundle, construct `AuthManager` with shared refresh admission, and adapt newer timeout keywords for older callables.

`AuthManager` already owns refresh necessity, token protocol, persistence, singleflight, and permanent-error deactivation. The proxy methods are a distinct orchestration boundary around that owner. Error classification and account-health mutation remain active in unfinished failover changes and must not move with freshness.

Earlier proxy decomposition removed the service-module `RefreshError` import after production catch sites moved to internal modules. Three HTTP bridge integration fakes still construct that exception through `app.modules.proxy.service`, and the repository HEAD exposed it. This change restores that explicit facade seam rather than changing the exception contract.

## Goals / Non-Goals

**Goals:**

- Give shared provider-aware credential freshness one typed runtime owner outside the proxy facade.
- Preserve API-key bypass, OAuth freshness, force refresh, repository lifetime, AuthManager singleflight, refresh admission, and timeout ContextVar restoration.
- Preserve inherited `ProxyService` method names and instance/class monkeypatch behavior, including older `_ensure_fresh` fakes without `timeout_seconds`.
- Preserve `AuthManager` and provider-constant facade exports and restore the pre-decomposition `RefreshError` export.
- Ratchet ownership and dependency direction and add direct coverage for currently implicit timeout and compatibility behavior.

**Non-Goals:**

- Change refresh intervals, OAuth protocol, stored token persistence, singleflight keys, cooldown, deactivation policy, or admission limits.
- Move or change account error classification, health mutation, selection, routing, failover, or availability probes.
- Change any transport request/retry state machine.
- Restart, drain, retarget, or otherwise modify the primary backend or Caddy during preflight.

## Decisions

1. Add `_AccountFreshnessMixin` and a structural `_AccountFreshnessService` protocol in `_service/account_freshness.py`. The protocol exposes the repository factory, work-admission controller, and inherited `_ensure_fresh` dispatch used by the compatibility wrapper.
2. Move `_ensure_fresh` and `_ensure_fresh_with_budget` together. The first owns refresh execution; the second is the cross-transport compatibility entry point. Separating them would obscure the supported injection contract.
3. Keep `AuthManager` as the canonical owner of refresh decisions, singleflight, admission acquisition during actual refresh, persistence, and deactivation. The runtime only constructs it with `repos.accounts` and the existing admission callback.
4. Keep the API-key provider check before timeout push and repository entry. API-key credentials require neither OAuth repositories nor refresh admission.
5. Keep timeout push immediately around the repository/AuthManager lifecycle and reset it in `finally`, including exception and cancellation paths. Nested caller overrides must be restored rather than cleared.
6. Keep `_ensure_fresh_with_budget` dispatching through `self._ensure_fresh` and `_call_with_supported_optional_kwargs`. This retains instance/class monkeypatching and filters only the newer optional `timeout_seconds` keyword.
7. Re-export `AuthManager`, `ACCOUNT_PROVIDER_API_KEY`, and `RefreshError` explicitly from `service.py` and ratchet them as facade names. The first two are existing test seams; the third restores the repository-HEAD compatibility surface lost during earlier extraction.
8. Leave all error-health methods in `service.py`. Moving them now would overlap `deterministic-failover-soft-drain`, selected-model failover, provider fail-closed, and routing work.

## Risks / Trade-offs

- [Risk] Moving ContextVar handling can leak a request timeout into later refreshes. -> Preserve the exact token/reset `finally` boundary and test success, exception, and cancellation against an outer override.
- [Risk] Calling a directly imported method can bypass instance/class test replacements. -> The budget wrapper continues to resolve `self._ensure_fresh` at call time.
- [Risk] Provider bypass can move after repository/admission work. -> Keep it as the first branch and assert no repository or admission capability is touched.
- [Risk] Restoring an exception export could be mistaken for retaining dead production logic. -> Re-export the canonical exception class only; no duplicate implementation or catch path is added.
- [Trade-off] Three explicit facade aliases remain. -> They are proven current compatibility seams and are architecture-ratcheted instead of left accidental.

## Migration Plan

1. Add the typed freshness runtime and direct provider/timeout/compatibility tests.
2. Inherit the mixin, remove the two local methods and their runtime-only imports, and preserve the three explicit facade aliases.
3. Add architecture ownership checks and run direct plus all-transport focused regressions against the recorded pre-change baseline.
4. Validate OpenSpec and an isolated backend on dedicated ports while leaving Caddy and the primary backend untouched.

Rollback is a source-level move of the two methods back into `service.py`; no API, database, credential, configuration, or persisted-state migration is involved.

## Open Questions

None.
