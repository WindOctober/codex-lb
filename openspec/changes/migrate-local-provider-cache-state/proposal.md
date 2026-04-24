## Why

The local codex-lb runtime has provider-account, news-cache, and scholar-cache behavior living in a local package/state directory. Keeping those changes only inside `.venv` and `var` makes the setup hard to maintain, review, or move to another machine.

## What Changes

- Add source-controlled API-key upstream provider support to the fork, including account metadata, provider probing, routing priority, and provider-aware proxy forwarding.
- Add fork-native React pages for the existing news and scholar cache snapshots instead of copying the old static frontend.
- Restore the local runtime compatibility behaviors that materially change operator UX: News/Scholar auto-refresh workers, dashboard domain grouping, request session-status diagnostics, provider websocket HTTP fallback, image-generation tool passthrough, stream idle-timeout diagnostics, and the stale usage-quota clearing behavior.
- Add a repeatable state migration script that copies the current local runtime DB, encryption key, and cache files into the fork runtime directory without touching the running source environment.
- Add Alembic revisions that let a DB already stamped at the local provider branch upgrade to the fork head.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `frontend-architecture`: Accounts, News, and Scholar dashboard UI are extended.
- `proxy-runtime-behavior`: provider transport compatibility, diagnostic endpoints, and quota handling now match the migrated local runtime semantics.
- `database-migrations`: Alembic accepts the local provider-account branch and merges it into the fork head.
- `runtime-portability`: local runtime state can be copied into a portable fork-owned `var/` directory.

## Impact

- Affects account APIs, proxy routing, Alembic migrations, and dashboard source code.
- Adds `scripts/migrate_local_state.py` for repeatable local-state import.
- Keeps the original `/home/work/tools/codex-lb` runtime untouched.
