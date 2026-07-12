## Why

Operators need to refresh or replace OAuth credentials for an existing OpenAI account before it is deactivated. Today re-authentication is effectively tied to automatic same-identity matching, which prevents intentionally replacing a stored account identity while preserving its local usage history, routing configuration, assignments, and sticky data.

## What Changes

- Allow starting OAuth re-authentication for a selected existing OpenAI OAuth account regardless of its current active/paused/deactivated status.
- When a selected account is re-authenticated, persist the newly authorized OpenAI identity and tokens onto that selected account instead of requiring the new identity/email to match the stored account.
- Preserve local account data and routing metadata owned by the selected account while replacing upstream OAuth identity fields.
- Keep non-targeted OAuth/import behavior unchanged, including existing ambiguity checks for same-email different-identity imports.

## Capabilities

### New Capabilities

- `account-reauthentication`: OAuth re-authentication behavior for existing accounts and targeted identity replacement.

### Modified Capabilities

- `frontend-architecture`: Accounts UI action availability and OAuth start payload behavior.

## Impact

- Backend OAuth API request schema and state handling.
- Account repository re-auth persistence logic.
- Accounts frontend action menu and OAuth dialog invocation.
- Integration and frontend tests for targeted re-authentication.
