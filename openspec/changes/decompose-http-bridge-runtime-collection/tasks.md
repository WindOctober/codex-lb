## 1. Runtime Collection Extraction

- [x] 1.1 Add a typed HTTP bridge runtime collection mixin and live-state capability protocol.
- [x] 1.2 Move request-status and per-account runtime collection without changing match or count semantics.
- [x] 1.3 Move complete dashboard snapshot, health, capacity, and egress collection without changing contracts or fallbacks.
- [x] 1.4 Inherit the mixin from `ProxyService`, remove local method bodies, and retain runtime contract compatibility exports.
- [x] 1.5 Ratchet architecture ownership and dependency-direction tests.

## 2. Verification

- [x] 2.1 Run focused runtime contract, request-status, account occupancy, health, capacity, and egress tests.
- [x] 2.2 Run dashboard, accounts, and request-log diagnostics API tests.
- [x] 2.3 Run formatting, lint, compilation, type-boundary, architecture, and diff checks.
- [x] 2.4 Validate the OpenSpec change and main specs.
- [x] 2.5 Validate on an isolated backend without restarting, draining, or retargeting the primary backend or Caddy.
