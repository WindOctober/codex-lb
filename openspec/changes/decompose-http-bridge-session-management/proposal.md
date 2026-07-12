## Why

HTTP bridge session registry mutation, durable lifecycle, capacity reclamation, upstream creation, and session acquisition remain interleaved in the proxy orchestration service. This makes account binding, continuity ownership, concurrency leases, and replacement behavior difficult to reason about independently and keeps `ProxyService` as a large shared failure domain.

## What Changes

- Introduce a typed HTTP bridge session-management boundary.
- Keep pure reuse, eligibility, pressure, and key-selection rules in a canonical policy module.
- Move registry and durable lifecycle operations behind an explicit lifecycle capability boundary.
- Move capacity accounting, reclamation, and shard selection behind an explicit capacity boundary.
- Decompose session creation and acquisition into focused orchestration modules without changing inherited service method names.
- Preserve existing monkeypatch-compatible adapters only where they are part of the tested internal contract.

## Impact

- Affected code: proxy service, HTTP bridge policy, lifecycle, capacity, session creation/acquisition, architecture tests, and focused bridge tests.
- No API, database, routing, account-selection, durable continuity, queue, concurrency, event, or error-contract changes.
