## Why

Ended Codex process trees remain visible as unread until a user opens each one. Under high parallel runs this creates noisy process lists and requires repetitive manual cleanup.

## What Changes

- Add a Processes page action that marks all unread ended process trees as read and clears them from the retained list.
- Add a backend bulk endpoint for the action so cleanup is consistent across refreshes.

## Impact

- Affects the dashboard Processes page and `/api/processes` endpoints.
- No database migration is required because process retention state is in memory.
