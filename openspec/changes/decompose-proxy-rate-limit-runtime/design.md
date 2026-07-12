## Context

`ProxyService` currently owns both request transport orchestration and the rate-limit read model consumed by response headers and the dashboard endpoint. The read model opens its own repository bundle, optionally refreshes usage, substitutes latest-model additional quota rows for weekly limits, aggregates credits, and projects arbitrary additional limits. It shares no mutable request state with streaming or bridge code.

## Goals / Non-Goals

**Goals:**

- Move the complete rate-limit read model behind a typed mixin boundary.
- Keep `rate_limit_headers()` and `get_rate_limit_payload()` available on `ProxyService` through inheritance.
- Preserve repository transaction lifetime, cache behavior, latest-model substitution, and aggregation order.
- Keep helper ownership and dependencies explicit enough for focused testing.

**Non-Goals:**

- Change usage refresh policy or make header computation trigger refresh.
- Change account eligibility, averaging, reset-time selection, credits, or additional-limit availability.
- Deduplicate the separate user-facing usage module in this stage.
- Change API schemas or endpoint wiring.

## Decisions

### Use one `_RateLimitRuntimeMixin`

The public header and payload methods share latest-row and additional-limit helpers, so one cohesive mixin keeps the full read model together. A typed protocol requires only `_repo_factory`, which avoids importing or inspecting `ProxyService`.

Alternative considered: split headers and dashboard payload into separate modules. They would duplicate usage-row and account-selection capabilities and create a less coherent boundary.

### Move implementation dependencies with the read model

The extracted module imports usage normalization, rate-limit projection helpers, quota-key registry helpers, cache access, and `UsageUpdater` directly. These are implementation dependencies, not service customization points, and repository-wide audit found no legacy monkeypatches through `service.py`.

Alternative considered: retain a facade for every helper in `service.py`. That would preserve accidental coupling and leave the service import surface unnecessarily large.

### Preserve method names through inheritance

Callers and focused tests continue to access the same methods on `ProxyService`. The architecture test requires the mixin and rejects local redefinitions.

## Risks / Trade-offs

- [Risk] A hidden caller patches a helper through `service.py`. -> Keep public service methods stable, run repository-wide reference audit, and preserve only demonstrated compatibility seams.
- [Risk] Repository scope changes during extraction. -> Move method bodies mechanically and retain the same `async with self._repo_factory()` boundaries.
- [Risk] Additional quota ordering or availability changes. -> Run focused additional-limit tests and API payload tests against fixed fixtures.

## Migration Plan

1. Add the typed module and copy the rate-limit methods without semantic edits.
2. Inherit the mixin from `ProxyService` and remove local method bodies.
3. Ratchet architecture ownership tests.
4. Run static, unit, integration, and isolated-backend checks.

Rollback is a source-level move back into `service.py`; no persisted state or configuration changes are involved.

## Open Questions

None.
