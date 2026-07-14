## Why

The upstream usage payload can now omit a distinct five-hour window and return only a weekly window, while codex-lb can continue enforcing an older stored five-hour snapshot. That stale gate can incorrectly remove otherwise usable accounts and surface `No active accounts available` even while upstream capacity remains.

## What Changes

- Add a persisted dashboard setting that lets operators ignore five-hour usage-snapshot enforcement for account routing.
- Expose the setting as a compact control on the Accounts page.
- Preserve weekly quota enforcement, explicit upstream rate-limit cooldowns, account state, model compatibility, API-key scope, and local concurrency admission when the setting is enabled.
- Treat a weekly-only upstream usage payload as authoritative for the current generic quota shape so stale five-hour snapshots do not continue to drive routing or dashboard status.
- Add regression coverage for weekly-only payloads, explicit 429 preservation, settings persistence, and Accounts-page behavior.

## Capabilities

### New Capabilities

- `optional-five-hour-limit-routing`: Defines the operator setting and routing behavior for optionally ignoring five-hour usage snapshots while preserving all other admission controls.

### Modified Capabilities

- `usage-refresh-policy`: Defines how a successful weekly-only upstream snapshot supersedes stale generic five-hour usage state.

## Impact

- Dashboard settings schema, persistence, API, cache, and migration.
- Proxy account-state construction and selection inputs.
- Generic usage refresh/storage and Accounts dashboard usage mapping.
- Accounts frontend settings contract, control, mocks, and tests.
