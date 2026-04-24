## 1. Provider accounts

- [x] 1.1 Add provider-account DB fields and Alembic merge path.
- [x] 1.2 Add API-key upstream provider creation, availability probing, priority update, and account summaries.
- [x] 1.3 Make proxy routing and upstream clients honor provider base URLs, wire APIs, and supported model lists.
- [x] 1.4 Add dashboard UI to create providers, inspect routing, test availability, and update priority.

## 2. News and scholar cache UI

- [x] 2.1 Add cache-backed backend services and dashboard routes for news and scholar snapshots.
- [x] 2.2 Add React pages backed by the fork frontend source.
- [x] 2.3 Reuse migrated cache files from the fork `var/` directory.
- [x] 2.4 Restore the local News/Scholar auto-refresh workers and supporting scholar topic cache.

## 3. Local runtime compatibility

- [x] 3.1 Restore provider websocket compatibility and HTTP fallback for non-codex wire APIs.
- [x] 3.2 Preserve built-in tool passthrough for `image_generation` and other non-function tools needed by the migrated local runtime.
- [x] 3.3 Add request session-status diagnostics and dashboard grouped-account responses that match the migrated local dashboard behavior.
- [x] 3.4 Restore stream idle-timeout debug logging and stale quota-clearing behavior used by the local runtime.

## 4. Runtime state migration

- [x] 4.1 Add a script that copies local DB, encryption key, news cache, and scholar cache into fork `var/`.
- [x] 4.2 Ignore fork runtime `var/` state in git.
- [x] 4.3 Run Alembic upgrade against the copied fork DB and verify no schema drift remains.

## 5. Verification

- [x] 5.1 Run Python compile checks.
- [x] 5.2 Run Alembic policy and schema drift checks on the copied DB.
- [x] 5.3 Run frontend typecheck/build.
- [x] 5.4 Run the repository pytest suite after the compatibility migration.
- [ ] 5.5 Run `openspec validate --specs` (blocked locally: `openspec` CLI is not installed in this environment).
