## ADDED Requirements

### Requirement: Processes bulk unread cleanup

The Processes page SHALL provide a single action that marks all unread ended process trees as read and removes them from the retained process list.

#### Scenario: Clear unread ended process trees

- **GIVEN** the Processes page has one or more unread ended process trees
- **WHEN** the user activates the bulk clear action
- **THEN** the frontend calls the bulk process cleanup API
- **AND** the process tree list refreshes
- **AND** the unread ended process trees no longer appear in the retained list.

#### Scenario: No unread ended process trees

- **GIVEN** the Processes page has no unread ended process trees
- **WHEN** the page renders the bulk clear action
- **THEN** the action is disabled.
