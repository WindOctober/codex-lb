## MODIFIED Requirements

### Requirement: Local runtime state can be copied without mutating the source environment
The project SHALL provide a repeatable operator path for copying local codex-lb runtime state into the fork runtime directory without deleting or modifying the running source environment.

#### Scenario: Copy local runtime state into fork var directory
- **GIVEN** the local runtime state directory contains `store.db`
- **WHEN** an operator runs the migration script
- **THEN** the script copies `store.db` into the fork runtime directory
- **AND** it copies `encryption.key`, `news-cache.json`, and `scholar-cache.json` when present
- **AND** it validates that provider-account columns exist in the copied DB
- **AND** it leaves the source runtime state untouched
