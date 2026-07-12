## ADDED Requirements

### Requirement: Capacity retries preserve the model and rotate accounts

When an HTTP-bridged Responses request receives an account-limit or model-capacity failure before meaningful downstream output, the proxy MUST preserve the requested model and retry through a different eligible account. The account that produced the failure MUST be excluded from that immediate retry. The proxy MUST NOT substitute another model.

#### Scenario: Server overload rotates accounts without changing models

- **GIVEN** an HTTP bridge request for model `M` is assigned to account `A`
- **AND** account `B` is eligible for model `M`
- **WHEN** upstream returns `server_is_overloaded` before any text output
- **THEN** the proxy retries through account `B`
- **AND** the retried payload still requests model `M`

#### Scenario: Selected-model capacity rotates accounts

- **GIVEN** an HTTP bridge request receives `Selected model is at capacity. Please try a different model.` before any text output
- **WHEN** another eligible account supports the requested model
- **THEN** the failing account is excluded from the retry
- **AND** the original model remains unchanged

#### Scenario: Account usage limit rotates accounts

- **GIVEN** an HTTP bridge request receives `usage_limit_reached`
- **WHEN** another eligible account supports the requested model
- **THEN** the proxy retries through another account with the same model

#### Scenario: Generic transient failure may retain the account

- **GIVEN** an HTTP bridge request receives a generic transient server or transport failure that is not a capacity signal
- **WHEN** the proxy reconnects before downstream visibility
- **THEN** it MAY retry the same account
