## Context

The earlier runtime-observability decomposition moved immutable snapshot contracts, health classification, session aggregation, capacity mathematics, and final snapshot construction into `http_bridge/runtime.py`. `ProxyService` still performs the stateful half: it locks and samples live sessions, searches pending requests, loads latency data and active accounts, reads egress state, and passes immutable observations into the pure helpers.

These methods serve dashboard, account-list, and request-log diagnostics. They do not mutate bridge lifecycle state and share a narrow set of live-state and repository capabilities.

## Goals / Non-Goals

**Goals:**

- Move stateful runtime collection behind a typed HTTP bridge mixin boundary.
- Keep existing public service method names and snapshot type exports compatible.
- Preserve lock acquisition, copy-before-iteration behavior, pending-lock scope, observation fields, fallback values, and output ordering.
- Keep pure runtime contracts and calculations in the existing `runtime.py` module.

**Non-Goals:**

- Change dashboard schemas, health thresholds, sampling defaults, or refresh cadence.
- Change HTTP bridge lifecycle, session mutation, account routing, or capacity enforcement.
- Add caching, new database queries, or background snapshot collection.
- Merge request-log model rewriting or durable account validation into this boundary.

## Decisions

### Add `runtime_collection.py` beside the pure runtime module

The new module owns state access and repository orchestration, while `runtime.py` remains a pure contract/calculation module. This preserves the existing dependency direction: collection depends on pure runtime, never the reverse.

Alternative considered: add the mixin directly to `runtime.py`. That would mix I/O and live mutable state with the pure module established by the prior change.

### Use a typed service capability protocol

The mixin protocol exposes the bridge lock, live session and inflight maps, and repository factory. This makes state ownership explicit without importing or inspecting `ProxyService`.

Alternative considered: pass snapshot callbacks into every method. That would add indirection without reducing required state capabilities.

### Preserve the existing lock choreography

The collection layer copies the global session maps under `_http_bridge_lock`, then acquires each session's `pending_lock` independently. No repository I/O occurs while the global bridge lock is held.

Alternative considered: collect a strongly consistent snapshot under one lock. That would increase hot-path contention and change current behavior.

### Move egress projection with collection

Egress projection reads process-global runtime/configuration state and converts it to the existing immutable snapshot. It belongs to state collection rather than the pure aggregation module.

## Risks / Trade-offs

- [Risk] Protocol fields drift from `ProxyService` initialization. -> Ratchet architecture tests and run runtime methods against a real service instance.
- [Risk] Lock scope changes create contention or inconsistent counts. -> Copy method bodies mechanically and retain lock nesting exactly.
- [Risk] Dashboard fallback values change when repository queries fail. -> Add focused health and capacity failure tests and run existing runtime tests.
- [Risk] Compatibility imports disappear. -> Keep runtime data classes imported by `service.py` and test identity with canonical contracts.

## Migration Plan

1. Add the typed collection module and move egress projection plus the five runtime collection methods without semantic edits.
2. Inherit the mixin from `ProxyService`, remove local implementations, and retain canonical runtime contract exports.
3. Ratchet architecture ownership tests and run runtime/dashboard/account diagnostics tests.
4. Run static and isolated-backend verification while leaving the primary backend and Caddy untouched.

Rollback is a source-level move back into `service.py`; no database, configuration, or persisted-state migration is involved.

## Open Questions

None.
