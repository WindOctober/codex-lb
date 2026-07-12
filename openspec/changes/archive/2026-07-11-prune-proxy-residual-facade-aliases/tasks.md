## 1. Residual Facade Alias Cleanup

- [x] 1.1 Audit exact code, test, string-injection, architecture, and OpenSpec references; record pre-change file fingerprints.
- [x] 1.2 Freeze a focused pre-change budget and rate-limit canonical-owner behavior matrix.
- [x] 1.3 Remove only the two zero-reference service re-export imports while leaving canonical owners unchanged.
- [x] 1.4 Extend the proxy absent-name architecture ratchet for both aliases.

## 2. Verification

- [x] 2.1 Rerun the identical canonical-owner matrix and verify the facade aliases are absent.
- [x] 2.2 Run formatting, lint, compilation, scoped type, reference, import, and diff checks.
- [x] 2.3 Strictly validate the OpenSpec change and validate all main specs.
- [x] 2.4 Validate on isolated backend/mock ports, clean only those processes, and confirm Caddy and the primary backend remain healthy with unchanged PIDs.
