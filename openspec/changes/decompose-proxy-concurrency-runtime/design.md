## Context

`ProxyService` owns two `AccountModelConcurrencyLimiter` instances: one shared by ordinary request and HTTP bridge connect work, and one dedicated to durable HTTP bridge sessions. The limiter implementation is already focused, but the service still owns all policy methods that read limits, project saturated account sets, acquire and release leases, wait for a bridge connect slot, and construct the local overload response.

These methods are consumed by ordinary streaming, HTTP bridge creation/capacity, WebSocket request finalization, and account selection. Their shared invariant is local account/model admission, not transport orchestration.

## Goals / Non-Goals

**Goals:**

- Move local account/model concurrency policy behind one typed mixin boundary.
- Preserve the two-limiter topology and all method names through inheritance.
- Preserve limit normalization, saturation keys, lease idempotence, wait cadence, request-budget termination, logging, and overload payloads.
- Preserve existing `service.py` settings and budget monkeypatch seams.

**Non-Goals:**

- Change default or configured limits.
- Change account selection ordering or add a new retry/fallback policy.
- Merge request, connect, and session budgets.
- Change the underlying limiter implementation or metrics.

## Decisions

### Use a `_ConcurrencyRuntimeMixin`

One mixin owns all methods that operate on the two limiter instances. A typed protocol exposes those limiters plus the existing settings and remaining-budget capabilities, keeping the module independent of `ProxyService`.

Alternative considered: move methods into `account_concurrency.py`. That module currently contains reusable state primitives; adding service settings, proxy errors, and request-state behavior would mix policy with the reusable limiter.

### Preserve shared request/connect limiter state

Ordinary requests and HTTP bridge connection establishment continue to use `_account_model_concurrency`; durable bridge sessions continue to use `_http_bridge_account_model_sessions`. The extraction moves references, not state ownership or initialization.

### Keep the connect wait loop polling behavior

The 50 ms bounded polling loop remains unchanged and uses `_remaining_budget_seconds_compatible()` so existing service-level tests can control time/budget behavior.

Alternative considered: add a condition variable to the limiter. That could improve wakeup latency but changes the limiter contract and belongs in a separate behavior change.

## Risks / Trade-offs

- [Risk] A transport uses the wrong limiter after extraction. -> Test ordinary, connect, and session counters independently and together.
- [Risk] Lease release ceases to be idempotent. -> Retain request-state clearing before `release()` and run repeated-finalization tests.
- [Risk] Existing settings patches no longer affect limits. -> Resolve limits through `_proxy_runtime_settings()` and run the full concurrency suite.
- [Risk] Connect wait no longer honors total budget. -> Resolve time through `_remaining_budget_seconds_compatible()` and test slot release plus exhaustion.

## Migration Plan

1. Add the typed concurrency runtime and copy policy methods without semantic edits.
2. Inherit the mixin, remove local methods, and retain limiter initialization in `ProxyService.__init__`.
3. Ratchet architecture ownership and run the complete account-model concurrency suite plus bridge regressions.
4. Run static and isolated-backend verification without touching the primary backend or Caddy.

Rollback is a source-level move back into `service.py`; no persisted state or configuration migration is involved.

## Open Questions

None.
