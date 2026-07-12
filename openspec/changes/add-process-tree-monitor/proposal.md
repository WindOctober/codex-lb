# Add Process Tree Monitor

## Why

Operators need a dashboard-adjacent view that shows currently running Codex CLI process trees without interrupting existing sessions. The view should make it clear which root process owns each tree, what path it is working in, and which child processes are attached.

## What Changes

- Add a read-only process tree API for Codex-related local processes.
- Add a `Processes` frontend surface alongside `Dashboard`.
- Poll process state at a conservative interval so the view stays current without adding noticeable load.
- Redact/summarize command lines instead of exposing full prompts or secrets.

## Impact

- New backend module: `app/modules/processes`.
- New frontend feature folder: `frontend/src/features/processes`.
- New navigation route: `/processes`.
