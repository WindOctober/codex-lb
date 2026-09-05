## 1. Implementation
- [x] Add Astra bootstrap metadata and pricing.
- [x] Add model discovery, request compatibility, filtering, and pricing regression coverage.
- [x] Synchronize the normative spec and operational context.

## 2. Validation and rollout
- [x] Run focused tests, lint, and OpenSpec validation.
- [x] Verify health, dashboard, model discovery, and a real Astra request on an isolated non-primary backend.
- [x] Stop the isolated backend and restart only backend 2456.
- [x] Verify gateway 2455 and backend 2456 health and Astra discovery.
- [x] Archive the verified change and record the rollout result.
