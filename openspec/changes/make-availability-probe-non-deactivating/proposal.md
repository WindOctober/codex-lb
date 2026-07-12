# Change: Make availability probe non-deactivating

## Motivation

Operators use the account availability action as a diagnostic check. The current probe force-refreshes OAuth tokens and persists `deactivated` when the refresh endpoint returns a permanent token error, even though the upstream ChatGPT account may still be usable or the stored token material may simply be stale.

## Scope

- Keep production token refresh behavior unchanged for request routing and background refresh paths.
- Make the manual account availability probe report refresh failures without writing `deactivated`.
- Continue to reactivate an account when a manual availability probe succeeds.

## Non-Goals

- Add a new account status.
- Change usage refresh deactivation policy.
