## Why

The Accounts page currently shows only the number of banked rate-limit resets, so operators cannot see when each reset opportunity expires or which one needs attention first. The upstream credit payload already carries deadline metadata that codex-lb can expose safely and present without revealing internal credit identifiers.

## What Changes

- Extend the account reset-credit status response with display-safe metadata for every available reset opportunity, including its grant time and expiry deadline when supplied upstream.
- Sort reset opportunities by expiry so the nearest deadline is consistently first.
- Add a compact deadline list to the account detail reset panel with exact local times and readable remaining-time/urgency labels.
- Preserve the existing reset-count and consume behavior, including graceful handling of credits whose upstream deadline metadata is absent.

## Capabilities

### New Capabilities

- `account-reset-credit-deadlines`: Defines the safe reset-credit deadline API contract and the account-detail deadline presentation.

### Modified Capabilities

None.

## Impact

- Backend usage payloads and account dashboard response schemas/services.
- Accounts frontend schemas, reset panel rendering, mocks, and tests.
- No database migration and no change to the upstream consume request.
