# Add Account Primary Drain Priority

## Why

Operators need a narrow `primary_drain` workflow where one or more selected accounts are deliberately consumed first. The current `primary_drain` strategy ranks every eligible account by recent 5h drain pressure, but there is no operator-controlled way to mark the account that should be drained while preventing other accounts from being consumed.

The routing strategy switch is also more relevant while working in the Accounts view than in Settings, because account-level drain priority is managed from account detail.

## What Changes

- Add a persistent per-account `primary_drain_priority_enabled` flag.
- In `primary_drain`, when any eligible scoped account has the flag enabled, account selection MUST restrict drain routing to flagged accounts before computing drain scores.
- Clear all drain-priority flags whenever dashboard routing strategy is changed away from `primary_drain`.
- Move the routing strategy control from Settings to Accounts.
- Add a star control in the account detail panel and a star indicator in the left account list.
- Disable and visually darken the star control outside `primary_drain`.

## Impact

- Adds one nullable-safe boolean column to `accounts`.
- Extends account summary/update contracts.
- Changes Accounts and Settings UI layout.
- Keeps `primary_drain` optional; default routing strategy remains unchanged.
