## 1. Backend Contract

- [x] 1.1 Parse optional reset-credit title, grant time, and expiry time from the upstream payload.
- [x] 1.2 Expose sorted display-safe available-credit metadata from the account reset-credit status endpoint without upstream IDs.
- [x] 1.3 Add backend contract and service tests for timestamp parsing, ordering, nullable metadata, and ID omission.

## 2. Accounts Interface

- [x] 2.1 Extend the frontend reset-credit schema and test mocks with per-credit deadline metadata.
- [x] 2.2 Render all reset opportunities in the account detail reset panel with exact local deadlines, remaining-time labels, urgency states, and a missing-deadline fallback.
- [x] 2.3 Add frontend coverage for deadline display and refresh after reset consumption.

## 3. Validation And Deployment

- [x] 3.1 Run focused backend lint/tests and frontend typecheck/tests/build.
- [x] 3.2 Validate the OpenSpec change and verify the built dashboard visually at desktop and mobile sizes.
- [x] 3.3 Validate an isolated non-primary backend, stop it by its explicit port, then restart only the primary backend and re-check both health endpoints.
