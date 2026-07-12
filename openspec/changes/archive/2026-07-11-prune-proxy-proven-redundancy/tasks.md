## 1. Proven Redundancy Cleanup

- [x] 1.1 Remove the audited zero-caller service wrappers, alias, constant, and their dedicated imports without changing canonical owners.
- [x] 1.2 Remove the unused HTTP bridge session-acquisition logger and logging import without changing structured observability.
- [x] 1.3 Fold the single-caller compact transport passthrough into the existing dynamic compatibility method.
- [x] 1.4 Add architecture ratchets requiring all removed redundant names to remain absent while preserving required facade names.

## 2. Verification

- [x] 2.1 Rerun the frozen affinity, model-support, session-acquisition, WebSocket previous-response, compact, architecture, and import matrix against the pre-change baseline.
- [x] 2.2 Run focused compact core-replacement, optional-keyword, provider fail-closed, and budget regressions.
- [x] 2.3 Run formatting, lint, compilation, type, import, reference, and scoped diff checks.
- [x] 2.4 Strictly validate the OpenSpec change and validate the main specs.
- [x] 2.5 Validate on isolated backend/mock ports, clean only that instance, and confirm Caddy and the primary backend remain healthy with unchanged PIDs.
