## 1. Account Freshness Runtime Extraction

- [x] 1.1 Add a typed account-freshness runtime mixin over explicit repository and work-admission capabilities.
- [x] 1.2 Move `_ensure_fresh` and `_ensure_fresh_with_budget` without changing provider bypass, AuthManager, admission, force, timeout, or optional-keyword behavior.
- [x] 1.3 Inherit the mixin, remove local method implementations and runtime-only imports, and preserve `AuthManager`, `ACCOUNT_PROVIDER_API_KEY`, and `RefreshError` facade aliases.
- [x] 1.4 Ratchet module ownership, mixin inheritance, facade compatibility names, and one-way dependency direction.
- [x] 1.5 Add direct tests for provider bypass, AuthManager capability wiring, nested timeout restoration on exception/cancellation, and legacy method signatures.

## 2. Verification

- [x] 2.1 Run focused freshness, admission, singleflight, compact, transcription, streaming, downstream WebSocket, and HTTP bridge regressions against the recorded pre-change baseline.
- [x] 2.2 Run all HTTP bridge refresh-failure facade cases and confirm the restored canonical `RefreshError` export removes the attributable compatibility regression.
- [x] 2.3 Run formatting, lint, compilation, type-boundary, architecture, import, and scoped diff checks.
- [x] 2.4 Strictly validate the OpenSpec change and validate the main specs.
- [x] 2.5 Validate on isolated backend/mock ports, clean only that instance, and confirm Caddy and the primary backend remain healthy with unchanged PIDs.
