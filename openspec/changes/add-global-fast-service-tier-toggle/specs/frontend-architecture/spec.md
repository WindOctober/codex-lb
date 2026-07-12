## ADDED Requirements

### Requirement: Accounts page exposes global fast mode control
The dashboard accounts UI SHALL provide a single control that enables or disables fast mode for all accounts. After the operation completes, the UI SHALL refresh account data and display each account's updated `fastServiceTierEnabled` state.

#### Scenario: Disable fast mode for all accounts
- **WHEN** an operator disables fast mode for all accounts
- **THEN** the app calls the bulk account fast-mode API with `enabled=false`
- **AND** the refreshed accounts list shows every account with `fastServiceTierEnabled=false`

#### Scenario: Enable fast mode for all accounts
- **WHEN** an operator enables fast mode for all accounts
- **THEN** the app calls the bulk account fast-mode API with `enabled=true`
- **AND** the refreshed accounts list shows every account with `fastServiceTierEnabled=true`
