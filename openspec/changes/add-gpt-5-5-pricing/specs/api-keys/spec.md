## ADDED Requirements

### Requirement: gpt-5.5 pricing is recognized

The system MUST recognize `gpt-5.5` pricing when computing request costs. Snapshot aliases for the same model family MUST resolve to the canonical `gpt-5.5` price table entry. For standard-tier and flex-tier requests with more than 272K input tokens, the system MUST apply the configured higher long-context rates.

#### Scenario: gpt-5.5 request priced at standard tier

- **WHEN** a request for `gpt-5.5` completes with standard service tier
- **THEN** the system computes cost using the configured `gpt-5.5` standard rates

#### Scenario: gpt-5.5 snapshot request priced at canonical rates

- **WHEN** a request for a `gpt-5.5-*` snapshot model completes
- **THEN** the system resolves the snapshot alias to `gpt-5.5`
- **AND** the system applies the same canonical rates

#### Scenario: gpt-5.5 long-context request priced at long-context rates

- **WHEN** a standard-tier `gpt-5.5` request completes with more than 272K input tokens
- **THEN** the system computes cost using the configured long-context `gpt-5.5` rates

### Requirement: Account usage summaries preserve persisted request costs

Account-level request usage summaries MUST sum the persisted `request_logs.cost_usd` value when it is present. For legacy rows whose persisted cost is absent, the summary MUST calculate only those rows from their recorded model, service tier, and token usage.

#### Scenario: Repriced historical request remains authoritative

- **GIVEN** a historical request log has a persisted `cost_usd` value
- **WHEN** the account request usage summary is calculated
- **THEN** the persisted value is included without recalculating that row from the current price table

#### Scenario: Mixed persisted and legacy request costs

- **GIVEN** an account has request logs with persisted costs and legacy logs without persisted costs
- **WHEN** the account request usage summary is calculated
- **THEN** persisted values are summed directly
- **AND** only legacy rows are calculated from recorded usage
