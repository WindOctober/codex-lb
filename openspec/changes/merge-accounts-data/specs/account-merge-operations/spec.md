## ADDED Requirements

### Requirement: Admin account data merge

The system SHALL allow an authenticated admin or local operator command to merge one existing source account into a different existing target account.

#### Scenario: Merge source data into target

- **WHEN** an admin merges source account `A` into target account `B`
- **THEN** usage history, additional usage history, request logs, sticky sessions, HTTP bridge session ownership, API key account assignments, and account group memberships that referenced `A` now reference `B`
- **AND** duplicate API key account assignments and duplicate account group memberships are collapsed instead of failing the merge
- **AND** account `A` is deleted
- **AND** account `B` remains present

#### Scenario: Reject invalid merge inputs

- **WHEN** the source account does not exist, the target account does not exist, or the source and target IDs are the same
- **THEN** the merge is rejected and no account data is moved
