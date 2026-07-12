## Context

The compact endpoint is implemented as one large method on `ProxyService`. Its state machine is cohesive, but it depends on service-owned encryption, admission, account selection, refresh, load-balancer feedback, API-key settlement, and request logging. Existing integration tests also patch selected symbols in `app.modules.proxy.service`, so extraction must preserve those compatibility injection points.

## Goals / Non-Goals

**Goals:**

- Make compact orchestration independently owned and type-checkable.
- Keep `ProxyService.compact_responses` available through mixin inheritance.
- Preserve all retry, failover, budget, settlement, affinity, logging, and service-tier behavior.
- Retain thin service adapters where existing runtime/test injection depends on service-module globals.

**Non-Goals:**

- Change compact request or response contracts.
- Tune retry counts, backoff, request budgets, account eligibility, or routing policy.
- Change load-balancer, API-key accounting, persistence, or provider support semantics.
- Restart or drain the primary backend during preflight.

## Decisions

1. Add `_CompactRuntimeMixin` and a structural `_CompactRuntimeService` protocol in `app/modules/proxy/_service/compact.py`. This keeps orchestration cohesive while making dependencies explicit. A free function would require passing a large mutable context and would obscure existing service capabilities.
2. Keep mutable dependencies on `ProxyService`: encryptor, load balancer, work admission, repository-backed logging, and API-key settlement remain owned by their current components. The new mixin only coordinates them.
3. Route settings and the upstream compact call through thin service compatibility methods. This preserves existing monkeypatch and deployment injection behavior without importing `service.py` from the extracted module or creating a circular dependency.
4. Keep pure policy helpers in their current modules and import them directly from the compact runtime. Duplicate helper implementations are not introduced.
5. Ratchet architecture tests so the compact method cannot silently return to `service.py`, then verify with focused unit/integration suites and the isolated backend keepalive smoke test.
6. Validate compact wire support before installing attempt-local timeout overrides, and cover admission acquisition with the same cleanup boundary as the upstream call. This prevents pre-upstream terminal paths from leaking ContextVar state into later work on the same task.

## Risks / Trade-offs

- [Risk] A direct import in the new module bypasses a historical service-level injection point. -> Mitigation: use explicit compatibility methods for settings and the core compact transport, and run existing monkeypatch-heavy tests.
- [Risk] Moving a long state machine can accidentally alter exception or `finally` ordering. -> Mitigation: move the method as a block, preserve nested-loop structure, and run success, 401, 500, failover, overload, settlement, and logging regressions.
- [Trade-off] The protocol is intentionally broad because compact orchestration uses several established subsystems. This is still preferable to keeping the state machine in the facade; later changes can narrow individual capabilities without changing runtime behavior.
