## 1. Canonical Payload Policy

- [x] 1.1 Add a typed core module for payload-too-large errors, historical slimming, image detection, summaries, and JSON byte sizing.
- [x] 1.2 Replace the duplicate core upstream-client implementation while preserving its independent threshold seam.
- [x] 1.3 Add focused equivalence tests for historical images, tool outputs, suffix preservation, and error envelopes.

## 2. Proxy Runtime Diagnostics

- [x] 2.1 Extract request-state size enforcement and oversized diagnostic dumps behind a typed protocol.
- [x] 2.2 Preserve service-level thresholds, dump-directory monkeypatches, helper exports, logging, and 413 behavior through thin facades.
- [x] 2.3 Move image capability checks and payload summaries to the canonical policy and remove duplicate service bodies.
- [x] 2.4 Ratchet architecture tests to enforce canonical ownership and dependency direction.

## 3. Verification

- [x] 3.1 Run formatting, lint, compilation, type-boundary, and architecture checks.
- [x] 3.2 Run focused payload-policy, oversized-request, ordinary streaming, HTTP bridge, and WebSocket regressions against the known baseline.
- [x] 3.3 Validate OpenSpec artifacts.
- [x] 3.4 Validate on an isolated backend without changing the running primary backend.
