## 1. Response-Create Runtime Extraction

- [x] 1.1 Add a typed response-create runtime mixin over explicit service capabilities.
- [x] 1.2 Move HTTP bridge and shared WebSocket request-state preparation without changing payload or state semantics.
- [x] 1.3 Move response-create gate and work-admission acquisition while preserving cleanup ordering.
- [x] 1.4 Add dynamic size-enforcement compatibility hooks, inherit the mixin, remove local methods, and ratchet architecture ownership.

## 2. Verification

- [x] 2.1 Run focused request-state, metadata, fingerprint, serialization, slimming, oversized-payload, and threshold-seam tests.
- [x] 2.2 Run focused gate/admission cleanup plus HTTP bridge and WebSocket integration regressions.
- [x] 2.3 Run formatting, lint, compilation, type-boundary, architecture, and diff checks.
- [x] 2.4 Validate the OpenSpec change and main specs.
- [x] 2.5 Validate on an isolated backend without restarting, draining, or retargeting the primary backend or Caddy.
