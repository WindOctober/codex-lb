# Tasks

- [x] 1. Add structured retry-after metadata to balancer selection failures.
- [x] 2. Treat recoverable account-selection failures as waitable in HTTP bridge create/reconnect paths.
- [x] 3. Add targeted regression tests for recoverable waits.
- [x] 4. Validate targeted tests and isolated backend preflight.
- [x] 5. Run `openspec validate --specs` or record local validation blocker.

Validation note: `openspec` is not installed in PATH or `.venv/bin`, so spec validation could not run locally.
