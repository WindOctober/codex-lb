## Context

`ProxyService` currently owns four methods that implement the API-key usage reservation lifecycle across ordinary streaming, HTTP bridge, WebSocket, and compact Responses requests. They instantiate `ApiKeysService` from the request-scoped repository bundle, shield accounting work from cancellation, translate reservation enforcement errors, and finalize or release reservations based on terminal usage data.

These methods share no mutable transport state. Their consumers already depend on method contracts through typed transport protocols, making the implementation a cohesive extraction candidate.

## Goals / Non-Goals

**Goals:**

- Move reservation, release, compact settlement, and stream settlement behind one typed mixin boundary.
- Preserve method names and signatures on `ProxyService` through inheritance.
- Preserve reservation enforcement and public error translation.
- Preserve finalize-versus-release conditions, model fallback, cached token, service-tier, shielding, and warning semantics.

**Non-Goals:**

- Change API-key limits, pricing, reservation storage, or accounting transaction behavior.
- Combine compact and stream settlement algorithms in this stage.
- Introduce background settlement, retries, batching, or a new service lifetime.
- Change transport routing, account selection, request budgets, or response schemas.

## Decisions

### Use one `_ApiKeyUsageRuntimeMixin`

All four methods implement one reservation lifecycle and require only `_repo_factory`. A single mixin keeps reservation and settlement invariants together while preserving existing call sites in transport protocols.

Alternative considered: place reservation methods in each transport module. That would duplicate accounting logic and weaken the single source of truth.

### Retain separate compact and stream settlement paths

Compact responses expose typed usage directly, while stream settlement consumes `_StreamSettlement` and returns whether persistence succeeded. Keeping separate methods preserves their distinct contracts and avoids speculative abstraction.

Alternative considered: normalize both into a new generic settlement model. This could be useful later but would introduce conversion logic and behavioral risk during a structural extraction.

### Keep shielded repository work synchronous

Reservation enforcement and settlement remain awaited inside `CancelScope(shield=True)`. This preserves quota correctness and avoids changing behavior during cancellation or shutdown.

Alternative considered: queue settlement in the background. That changes durability and failure timing and is outside this phase.

## Risks / Trade-offs

- [Risk] Error translation changes while moving imports. -> Preserve exact exception branches and test both rate-limit and invalid-key translations.
- [Risk] A reservation is finalized when it should be released. -> Test compact and stream success/incomplete paths with exact arguments.
- [Risk] Existing transport tests patch methods on `ProxyService`. -> Preserve inherited method names and run representative monkeypatch paths.
- [Risk] A hidden circular dependency is introduced. -> Require the new module to depend on API-key, service-tier, support, and repository abstractions only, never `service.py`.

## Migration Plan

1. Add the typed API-key usage runtime module and copy method bodies without semantic edits.
2. Inherit the mixin from `ProxyService`, remove local definitions, and remove only imports proven unused.
3. Ratchet architecture tests and add focused lifecycle tests.
4. Run static, transport, integration, and isolated-backend verification without touching the primary backend or Caddy.

Rollback is a source-level move back into `service.py`; no persisted state or configuration migration is involved.

## Open Questions

None.
