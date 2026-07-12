## Context

`ProxyService` currently owns request-log persistence for every transport, the helper that projects stream preflight failures into the same schema, and a completed-request model rewrite with bounded retries for rows that arrive slightly later. The methods open repository bundles, shield persistence from caller cancellation, normalize optional session IDs, and isolate logging failures from request handling. None of this state participates in account selection, transport retries, streaming, or bridge ownership.

The session-ID normalizer used by these methods is currently defined in `websocket/events.py`, despite being consumed by ordinary HTTP and HTTP bridge paths. This creates an inverted ownership boundary and makes a logging extraction depend on WebSocket event internals.

## Goals / Non-Goals

**Goals:**

- Move request-log persistence, stream preflight error logging, and completed-request model rewriting behind a typed mixin boundary.
- Keep both method names and signatures available on `ProxyService` through inheritance.
- Preserve repository lifetime, cancellation shielding, field mapping, latency calculation, transport defaults, and failure isolation.
- Give session-ID normalization a transport-neutral owner while retaining the former import path for compatibility.

**Non-Goals:**

- Change request-log schemas, retention, indexing, aggregation, or API presentation.
- Change when callers emit success/error logs or how token settlement is computed.
- Change transport orchestration, account selection, retry budgets, or exception handling.
- Introduce queued or batched request-log persistence.

## Decisions

### Use one `_RequestLoggingMixin`

The two methods share the same repository factory and request-log contract, and the preflight helper is a thin specialization of the general writer. One focused mixin keeps that relationship explicit. A typed protocol exposes only `_repo_factory`, avoiding imports or runtime inspection of `ProxyService`.

Alternative considered: introduce a stateful request-log service instance. That would require constructor and dependency-injection changes for no behavioral benefit in this mechanical extraction.

### Keep persistence synchronous to request finalization

The current writer shields its repository transaction from caller cancellation and awaits completion. The extraction preserves that behavior rather than introducing a background queue, which would change durability and shutdown semantics.

Alternative considered: fire-and-forget logging. This could lower tail latency but risks losing terminal records and is outside this behavior-preserving stage.

### Preserve bounded model-rewrite polling

The public model rewrite keeps its existing delay sequence and opens a fresh repository bundle per attempt so a request-log row committed by another task can become visible. Blank inputs remain no-ops, and persistence failures remain isolated.

### Put session-ID normalization in `affinity.py`

Session and turn-state identifiers already belong to the affinity boundary. `websocket/events.py` will import the helper so the old module path remains available, while service, bridge, and logging code import it from the canonical owner.

Alternative considered: add a one-function identifiers module. That would add another module without establishing a stronger domain boundary than the existing affinity module.

## Risks / Trade-offs

- [Risk] A hidden test patches methods directly on `ProxyService`. -> Preserve method names through inheritance and test normal attribute lookup.
- [Risk] Moving normalization changes import compatibility. -> Re-export the canonical function from `websocket/events.py` and add architecture coverage for ownership.
- [Risk] Field mapping or cancellation behavior changes during the move. -> Copy method bodies mechanically and run focused repository plus transport-path tests.
- [Risk] Mixin order shadows the methods. -> Assert the expected mixin base and reject local redefinitions in the architecture test.

## Migration Plan

1. Canonicalize session-ID normalization in the affinity module with a compatibility import in WebSocket events.
2. Add the typed request-logging mixin and move both method bodies without semantic edits.
3. Inherit the mixin from `ProxyService`, remove local definitions, and ratchet architecture tests.
4. Run focused static, unit, integration, and isolated-backend verification while leaving the primary backend and Caddy untouched.

Rollback is a source-level move back into `service.py`; no database, configuration, or persisted-state migration is involved.

## Open Questions

None.
