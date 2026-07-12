# Add Account Reset Credit Controls

## Why

Operators can see account usage in codex-lb, but banked Codex rate-limit resets are currently only usable through newer Codex client surfaces. codex-lb already owns the stored ChatGPT account tokens, so admins should be able to inspect and explicitly consume one earned reset for a selected account from the Accounts dashboard.

## What Changes

- Add an account-level dashboard API to read available earned rate-limit reset credits.
- Add an account-level dashboard API to consume one earned reset credit with an idempotency key.
- Add an Accounts page action that shows the current reset count and requires confirmation before consuming a reset.
- Refresh account usage state after a successful consume response.

## Impact

- Affected specs: account-reset-credits
- Affected code: `app/core/clients/usage.py`, `app/modules/accounts/*`, `frontend/src/features/accounts/*`
