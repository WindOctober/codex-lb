## ADDED Requirements

### Requirement: Accounts page re-auth action availability

The Accounts page SHALL expose OAuth re-authentication for existing OpenAI OAuth accounts regardless of whether the account is active, paused, or deactivated.

#### Scenario: Existing OpenAI OAuth account can re-auth

- **GIVEN** an OpenAI OAuth account is selected on the Accounts page
- **WHEN** the operator chooses re-authentication
- **THEN** the OAuth dialog starts with the selected account ID as the re-auth target

#### Scenario: API-key provider accounts do not show OAuth re-auth

- **GIVEN** an API-key provider account is selected on the Accounts page
- **WHEN** account actions are rendered
- **THEN** OAuth re-authentication is not offered for that account
