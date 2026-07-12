## 1. Routing strategy contract

- [x] 1.1 Add an OpenSpec delta for first-class high-waterline routing and round-robin removal.
- [x] 1.2 Make high-waterline the backend/dashboard default.
- [x] 1.3 Remove round-robin from backend routing strategy validation and selection.
- [x] 1.4 Make high-waterline fall back to capacity-weighted when no account is above the remaining-usage average by the configured margin.

## 2. UI and persistence

- [x] 2.1 Update settings schemas, mocks, labels, and controls for the new strategy set.
- [x] 2.2 Add a forward migration for dashboard settings defaults and existing pristine rows.

## 3. Validation

- [x] 3.1 Run focused backend unit/integration tests for routing, settings, and migrations.
- [x] 3.2 Run focused frontend typecheck and non-jsdom schema/constants tests. Component tests are blocked locally by the current Node/jsdom ESM incompatibility.
- [ ] 3.3 Run OpenSpec validation if the CLI is available. Blocked locally: `openspec` command is not installed.
