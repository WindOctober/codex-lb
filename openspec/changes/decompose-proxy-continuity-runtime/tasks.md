## 1. Continuity Runtime Extraction

- [x] 1.1 Add a typed continuity runtime mixin over the existing repository factory and owner cache state.
- [x] 1.2 Move durable account validation with cloning, activity, model-support, and repository-failure semantics intact.
- [x] 1.3 Move previous-response owner remember/miss/resolve behavior and the bounded cache constant.
- [x] 1.4 Inherit the mixin from `ProxyService`, retain cache initialization and constant compatibility export, and remove local methods.
- [x] 1.5 Ratchet architecture ownership and dependency-direction tests.

## 2. Verification

- [x] 2.1 Run focused durable account binding and cache eviction tests.
- [x] 2.2 Run previous-response owner scoped hit, fallback, DB hit/miss, fail-closed, ordinary, HTTP bridge, and WebSocket tests.
- [x] 2.3 Run formatting, lint, compilation, type-boundary, architecture, and diff checks.
- [x] 2.4 Validate the OpenSpec change and main specs.
- [x] 2.5 Validate on an isolated backend without restarting, draining, or retargeting the primary backend or Caddy.
