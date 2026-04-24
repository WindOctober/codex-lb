## MODIFIED Requirements

### Requirement: Alembic as migration source of truth
The system SHALL use Alembic as the only runtime migration mechanism and SHALL NOT execute custom migration runners.

#### Scenario: Local provider branch upgrades to fork head
- **GIVEN** a copied local DB is stamped at `20260328_000000_add_upstream_provider_accounts`
- **WHEN** the fork runs Alembic upgrade to `head`
- **THEN** the provider branch is merged into the fork migration graph
- **AND** the DB reaches the fork head revision without schema drift
