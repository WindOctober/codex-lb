## ADDED Requirements

### Requirement: Durable bridge stores verifiable completed input metadata
The `http_bridge_sessions` table MUST store nullable metadata for the latest completed list-shaped input: item count and a stable full-input fingerprint. The migration adding these columns MUST be forward-only, idempotent for existing deployments, and compatible with SQLite and PostgreSQL.

#### Scenario: Durable bridge migration adds input metadata columns
- **WHEN** migrations are applied to a database containing `http_bridge_sessions`
- **THEN** nullable `latest_input_item_count` and `latest_input_full_fingerprint` columns exist
- **AND** existing durable bridge rows remain valid without backfill
