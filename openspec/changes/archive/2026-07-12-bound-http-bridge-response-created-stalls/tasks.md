## 1. Runtime Implementation

- [x] 1.1 Add the canonical response-created startup timeout setting and request-targeted receive deadline state.
- [x] 1.2 Make the HTTP bridge reader settle startup timeouts, retire ambiguous multiplexed bridges, and emit phase diagnostics.
- [x] 1.3 Extend pre-created recovery to replay one retained, prefix-verified full resend while preserving fail-closed continuation behavior.

## 2. Verification

- [x] 2.1 Add focused tests for startup deadline selection, metadata handling, established long responses, safe full-resend replay, unsafe continuations, and sibling retirement.
- [x] 2.2 Run focused proxy regressions, formatting, typing, and strict OpenSpec validation.
- [x] 2.3 Run an independent adversarial audit and resolve confirmed findings.

## 3. Runtime Preflight And Deployment

- [x] 3.1 Start an isolated non-primary backend and verify health, dashboard bridge runtime, and startup-timeout behavior without touching Caddy or the primary backend.
- [x] 3.2 Stop only the isolated backend after validation.
- [x] 3.3 Normally restart only the primary backend on port 2456 and verify both 2455 and 2456 health endpoints.
