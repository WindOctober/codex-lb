## ADDED Requirements

### Requirement: Account fast service tier control
The dashboard accounts UI MUST expose a per-account fast mode control. When enabled, the account update API MUST persist the setting and list responses MUST return the setting.

#### Scenario: Enable account fast mode
- **WHEN** an operator enables fast mode for an account from the accounts page
- **THEN** the account update API persists the setting
- **AND** subsequent account list responses include the enabled state
