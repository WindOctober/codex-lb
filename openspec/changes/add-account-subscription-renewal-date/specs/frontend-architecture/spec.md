## ADDED Requirements

### Requirement: Account detail exposes subscription renewal date

The Accounts UI MUST show an optional manually maintained subscription renewal date for each account. Operators MUST be able to set, update, and clear the date from the account detail panel without requiring a time-of-day value, and account list/detail refreshes MUST preserve the value returned by the account API.

#### Scenario: Operator saves renewal date

- **GIVEN** an account has no subscription renewal date
- **WHEN** an operator enters a renewal date and saves it from account detail
- **THEN** the account update API persists the date
- **AND** subsequent account summary responses include the date.

#### Scenario: Operator clears renewal date

- **GIVEN** an account has a subscription renewal date
- **WHEN** an operator clears it from account detail
- **THEN** the account update API persists a null date
- **AND** subsequent account summary responses return no renewal date.
