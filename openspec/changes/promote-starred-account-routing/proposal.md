# Promote Starred Account Routing

## Why

Operators use the account star control to force traffic toward selected accounts, but the current implementation only honors that flag inside `primary_drain`. Switching to another routing strategy can ignore or clear the starred accounts, which makes the control unreliable for active drainage and reset management.

## What Changes

- Treat starred accounts as a global pre-strategy priority pool for all routing strategies.
- Preserve the existing eligibility filters so paused, deactivated, rate-limited, quota-exceeded, and cooldown accounts are still skipped.
- Keep `primary_drain` focused on drain-score selection after the global starred-account priority step.
- Preserve starred account flags when changing routing strategies.

## Impact

- Any routing strategy prefers eligible starred accounts before applying its normal balancing logic.
- Routing strategy changes no longer clear account star state.
