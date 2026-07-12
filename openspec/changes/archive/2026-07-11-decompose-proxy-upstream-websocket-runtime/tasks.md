## 1. Shared Upstream WebSocket Runtime Extraction

- [x] 1.1 Add a typed upstream WebSocket runtime mixin over explicit encryption, admission, and factory capabilities.
- [x] 1.2 Move budgeted and unbudgeted upstream socket creation without changing provider checks, account transforms, timeout mapping, or lease cleanup.
- [x] 1.3 Add the local dynamic socket-factory compatibility method, inherit the mixin, and remove the obsolete facade methods and module-level wrapper.
- [x] 1.4 Ratchet module ownership, facade compatibility names, mixin inheritance, and one-way dependency direction.
- [x] 1.5 Add direct tests for factory parameter forwarding, provider rejection before admission, timeout mapping, and lease release on success, error, and cancellation.

## 2. Verification

- [x] 2.1 Run focused direct runtime, downstream WebSocket connection, and HTTP bridge creation/reconnect regressions against the recorded pre-change baseline.
- [x] 2.2 Run focused core transport and egress-selection regressions to confirm the extracted runtime does not absorb handshake or egress behavior.
- [x] 2.3 Run formatting, lint, compilation, type-boundary, architecture, and scoped diff checks.
- [x] 2.4 Strictly validate the OpenSpec change and validate the main specs.
- [x] 2.5 Validate on isolated backend/mock ports, clean only that instance, and confirm Caddy and the primary backend remain healthy with unchanged PIDs.
