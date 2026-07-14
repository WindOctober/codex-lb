## 1. Settings Contract

- [x] 1.1 Add the persisted `ignore_five_hour_limit` setting and forward-only database migration.
- [x] 1.2 Thread the setting through backend settings schemas, service, repository, API, cache invalidation audit, and contract tests.

## 2. Usage And Routing

- [x] 2.1 Retire stale generic primary history only after a successful weekly-only usage snapshot and preserve automatic recovery when primary data returns.
- [x] 2.2 Apply the override during every account-selection path while preserving weekly, additional quota, explicit 429, eligibility, and local admission controls.
- [x] 2.3 Add backend regression tests for weekly-only refresh, stale primary recovery, explicit rate-limit preservation, and selection behavior.

## 3. Accounts Interface

- [x] 3.1 Extend the frontend settings contract, payload builder, and mocks with the override.
- [x] 3.2 Add a compact accessible `Ignore 5h` control to the Accounts page and cover updates, loading, and failure behavior.

## 4. Validation And Deployment

- [x] 4.1 Run focused backend tests, lint/type checks, frontend tests/typecheck/build, and OpenSpec validation.
- [x] 4.2 Validate the migrated behavior on an isolated non-primary backend and stop only that explicit instance.
- [x] 4.3 Restart only the primary 2456 backend, enable the override, and verify 2455/2456 health plus successful account selection.
