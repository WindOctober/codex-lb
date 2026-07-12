## 1. API-Key Usage Runtime Extraction

- [x] 1.1 Add a typed API-key usage runtime mixin and repository-factory capability protocol.
- [x] 1.2 Move reservation enforcement and release without changing exception or shielding behavior.
- [x] 1.3 Move compact and stream settlement without changing finalize/release decisions or return contracts.
- [x] 1.4 Inherit the mixin from `ProxyService`, remove local method bodies, and remove only proven-unused imports.
- [x] 1.5 Ratchet architecture ownership and dependency-direction tests.

## 2. Verification

- [x] 2.1 Run focused reservation, error translation, release, compact settlement, and stream settlement tests.
- [x] 2.2 Run representative ordinary, compact, HTTP bridge, and WebSocket transport tests.
- [x] 2.3 Run formatting, lint, compilation, type-boundary, architecture, and diff checks.
- [x] 2.4 Validate the OpenSpec change and main specs.
- [x] 2.5 Validate on an isolated backend without restarting, draining, or retargeting the primary backend or Caddy.
