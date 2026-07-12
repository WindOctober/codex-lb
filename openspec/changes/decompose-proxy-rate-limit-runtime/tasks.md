## 1. Rate-Limit Runtime Extraction

- [x] 1.1 Add a typed rate-limit runtime mixin and repository-factory capability protocol.
- [x] 1.2 Move cached header and dashboard payload construction without changing contracts.
- [x] 1.3 Move usage refresh, latest-row substitution, credits, and additional-quota projection.
- [x] 1.4 Inherit the mixin from `ProxyService` and remove local method bodies while preserving method names.
- [x] 1.5 Ratchet architecture ownership and dependency-direction tests.

## 2. Verification

- [x] 2.1 Run focused rate-limit, additional-quota, cache, and API tests.
- [x] 2.2 Run formatting, lint, compilation, type-boundary, architecture, and diff checks.
- [x] 2.3 Validate the OpenSpec change.
- [x] 2.4 Validate on an isolated backend without restarting or draining the primary backend.
