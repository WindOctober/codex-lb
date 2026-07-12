# Tasks

- [x] Add upstream client methods for reading and consuming rate-limit reset credits.
- [x] Add accounts service/API endpoints for reset-credit status and consume.
- [x] Add frontend schemas, API functions, hooks, and account detail controls.
- [x] Add backend and frontend tests for reset-credit status, confirmation flow, and consume outcomes.
- [x] Run focused validation.

Validation notes:

- `.venv/bin/python -m pytest tests/unit/test_usage_client.py tests/unit/test_accounts_service.py` passed.
- `.venv/bin/ruff check app/core/clients/usage.py app/core/usage/models.py app/modules/accounts/api.py app/modules/accounts/service.py app/modules/accounts/schemas.py app/modules/usage/updater.py tests/unit/test_usage_client.py tests/unit/test_accounts_service.py` passed.
- `cd frontend && bun run typecheck` passed.
- `openspec validate add-account-reset-credit-controls --strict` blocked locally because `openspec` CLI is not installed.
- `cd frontend && bun run test src/__integration__/accounts-flow.test.tsx src/test/mocks/handler-coverage.test.ts` blocked before test execution by the existing Vitest/jsdom `html-encoding-sniffer` ESM loader error.
