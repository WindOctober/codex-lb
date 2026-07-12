## Why

Re-authenticating the same ChatGPT identity can create a new codex-lb account row when duplicate imports are enabled. Operators then lose continuity in dashboard usage, request logs, sticky sessions, and routing metadata unless they manually rewrite several account foreign keys.

## What Changes

- Add an admin account merge operation that moves data from one account ID to another account ID.
- Move usage history, additional usage history, request logs, sticky sessions, HTTP bridge session ownership, API key assignments, and account group memberships atomically.
- Delete the source account after its data has been moved.
- Provide a command-line entry point so an operator can perform a one-off merge against the live database without restarting codex-lb.

## Impact

- Code: `app/modules/accounts/*`
- Tests: repository/service/API coverage for account merge behavior
- Specs: new `account-merge-operations` capability
