# usage-refresh-policy Delta

## ADDED Requirements
### Requirement: Latest-model quota drives capacity calculations

When a latest model is configured and matching additional quota rows exist, account summaries, dashboard aggregate windows, proxy usage payloads, and gated-model routing budget decisions MUST use those latest-model additional quota rows instead of generic usage rows for the same account and window. When matching additional quota rows do not exist, the system MUST fall back to existing generic usage rows.

#### Scenario: Latest-model weekly row overrides generic weekly row
- **WHEN** the latest model is configured as `gpt-5.5`
- **AND** an account has a `gpt_5_5` secondary additional quota row
- **THEN** weekly calculations for that account use the `gpt_5_5` row

#### Scenario: Missing latest-model row falls back
- **WHEN** the latest model is configured
- **AND** an account has no matching latest-model additional quota row for a window
- **THEN** the system uses the account's existing generic usage row for that window if present

### Requirement: Dashboard grouped quota uses latest-model-eligible members

Dashboard domain-group quota percentages MUST aggregate only members that are not deactivated and whose plan is eligible for the configured latest model. Domain-group availability breakdowns MAY still count all members by status.

#### Scenario: Deactivated and unsupported members are excluded from grouped quota
- **WHEN** a dashboard domain group contains an active Plus account, an active Free account that does not support the latest model, and a deactivated paid account
- **THEN** the group's quota percentage and capacity totals are calculated from the active latest-model-eligible paid account only
- **AND** the availability breakdown still includes all three members

### Requirement: Latest-model configuration is manually updatable

The repository MUST provide a script that updates both the latest-model config file and the additional quota registry entry used to map model IDs to quota keys.

#### Scenario: Operator updates latest model
- **WHEN** an operator runs the latest-model update script with a model ID
- **THEN** `config/latest_model.json` records that model ID, quota key, and display label
- **AND** `config/additional_quota_registry.json` contains a `role: latest_model` entry for that model
