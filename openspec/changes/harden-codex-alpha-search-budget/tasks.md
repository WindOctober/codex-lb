## 1. Dedicated Search Budgets

- [x] 1.1 Add typed total-search and per-account-attempt timeout settings with example and local configuration.
- [x] 1.2 Use one dedicated absolute search deadline and cap each upstream account attempt by the smaller remaining/attempt budget.
- [x] 1.3 Preserve final upstream failures while retaining the stable total-budget error for true deadline exhaustion.

## 2. Regression Coverage

- [x] 2.1 Test that search can outlive the general proxy budget and receives the configured per-attempt cap.
- [x] 2.2 Test transient timeout failover and final-error preservation across two accounts.
- [x] 2.3 Run focused and related tests, Ruff, type checks, diff checks, and strict OpenSpec validation.
- [x] 2.4 Bound failure accounting and final request logging by the dedicated total search deadline and add regressions.
- [x] 2.5 Sanitize credential-refresh response bodies and add a public API regression for structured internal detail.
- [x] 2.6 Hard-observe cancellation-suppressing search bookkeeping under the remaining dedicated deadline and retain late persistence in the tracked shutdown registry.
- [x] 2.7 Reuse the parent's versioned prompt-cache affinity identity and filter non-Codex-wire providers before sticky resolution or attempt accounting.

## 3. Validation And Deployment

- [x] 3.1 Validate the combined audited build on backend-only port 3456 with a low-cost alpha search and Responses request.
- [x] 3.2 Restart only backend port 2456 and verify both health endpoints, search success, and fresh error logs without touching Caddy.
