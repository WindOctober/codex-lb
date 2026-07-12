## 1. Compact Runtime Extraction

- [x] 1.1 Add a typed compact runtime mixin over existing service capabilities.
- [x] 1.2 Move `compact_responses` and its compact-only constants without changing the state machine.
- [x] 1.3 Add thin settings and upstream-call compatibility hooks where service-module injection must remain stable.
- [x] 1.4 Inherit the mixin from `ProxyService`, remove the local implementation, and ratchet architecture ownership tests.
- [x] 1.5 Restore compact timeout overrides on pre-upstream terminal paths and ratchet provider/admission cleanup tests.

## 2. Verification

- [x] 2.1 Run focused compact success, budget, provider, 401, overload, logging, and service-tier tests.
- [x] 2.2 Run compact transient retry/failover, sticky-session, and API-key settlement integration tests.
- [x] 2.3 Run formatting, lint, compilation, type-boundary, architecture, and diff checks.
- [x] 2.4 Validate the OpenSpec change and main specs.
- [x] 2.5 Validate on an isolated backend without restarting, draining, or retargeting the primary backend or Caddy.
