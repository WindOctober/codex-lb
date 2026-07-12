## ADDED Requirements

### Requirement: Accounts page owns routing strategy selection

The Accounts page MUST expose the dashboard routing strategy control. The Settings routing section MUST NOT expose the routing strategy control.

#### Scenario: Operator switches to primary drain from Accounts

- **WHEN** an operator selects Primary drain on the Accounts page
- **THEN** the frontend saves `routingStrategy: "primary_drain"` through the settings API.

### Requirement: Account detail exposes drain-priority star

The account detail panel MUST expose a star toggle for `primary_drain` priority. The star control MUST be enabled only when the current routing strategy is `primary_drain`; outside `primary_drain`, it MUST be disabled and visually inactive.

#### Scenario: Starred account appears in list

- **GIVEN** the current routing strategy is `primary_drain`
- **AND** an account has `primaryDrainPriorityEnabled=true`
- **WHEN** the Accounts list is rendered
- **THEN** that account row shows an active star indicator.

#### Scenario: Non-drain strategy darkens stars

- **GIVEN** the current routing strategy is not `primary_drain`
- **WHEN** the Accounts page is rendered
- **THEN** account drain-priority stars are inactive and cannot be enabled.
