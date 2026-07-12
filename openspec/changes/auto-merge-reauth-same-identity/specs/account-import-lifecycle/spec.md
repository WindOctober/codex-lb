## ADDED Requirements

### Requirement: Re-auth merges same OpenAI identity

OpenAI OAuth import SHALL treat accounts as the same re-authenticated account only when the imported auth payload has the same normalized email and the same ChatGPT account ID as existing OpenAI OAuth account rows.

#### Scenario: Same identity re-auth updates one account

- **GIVEN** an existing OpenAI OAuth account with email `E` and ChatGPT account ID `A`
- **WHEN** an admin imports a new auth payload with email `E` and ChatGPT account ID `A`
- **THEN** the system updates the existing account instead of creating a `__copy` row
- **AND** the imported auth payload controls the account status, plan, tokens, and refresh timestamp
- **AND** local routing metadata such as KYC, configured priority, and account groups remain associated with the account

#### Scenario: Legacy same-identity copies are collapsed

- **GIVEN** multiple OpenAI OAuth account rows have the same normalized email and the same ChatGPT account ID
- **WHEN** an admin imports a new auth payload for that same email and ChatGPT account ID
- **THEN** the system merges account-owned data from the duplicate rows into one canonical account
- **AND** the duplicate account rows are removed
- **AND** the canonical account uses the imported auth payload status, plan, tokens, and refresh timestamp

#### Scenario: Same email different identity requires manual merge

- **GIVEN** an existing OpenAI OAuth account has email `E` and ChatGPT account ID `A`
- **WHEN** an admin imports a new auth payload with email `E` and ChatGPT account ID `B`
- **AND** `B` is different from `A`
- **THEN** the import is rejected with a conflict
- **AND** no automatic account merge or account copy is created
