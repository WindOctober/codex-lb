## 1. Connection

- [x] 1.1 Extract account selection, connect budget, 401 refresh, and failover decision methods.
- [x] 1.2 Preserve downstream connect timeout/failure emission and continuity fail-closed behavior.
- [x] 1.3 Ratchet the architecture boundary and run focused connection regressions.

## 2. Relay

- [x] 2.1 Extract upstream receive and text-event processing.
- [x] 2.2 Extract request finalization, expiry, pending failure, and terminal emission helpers.
- [x] 2.3 Preserve precreated replay, response ownership, account health, usage settlement, and downstream frame behavior.

## 3. Orchestration

- [x] 3.1 Extract downstream WebSocket lifecycle and request preparation.
- [x] 3.2 Preserve task cancellation and all lease cleanup paths.
- [x] 3.3 Ratchet the architecture boundary and prohibit reintroduction.

## 4. Verification

- [x] 4.1 Run formatting, lint, compilation, type-boundary, and architecture checks.
- [x] 4.2 Run focused and broad WebSocket regressions.
- [x] 4.3 Run HTTP bridge and ordinary streaming regressions affected by shared capabilities.
- [x] 4.4 Validate on an isolated backend and issue one low-cost real request before any primary restart.
- [x] 4.5 Re-check the primary backend and gateway after a backend-only restart.
- [x] 4.6 Validate OpenSpec artifacts if the CLI is available.
