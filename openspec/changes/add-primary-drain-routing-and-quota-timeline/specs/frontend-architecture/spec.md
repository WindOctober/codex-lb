## ADDED Requirements

### Requirement: Account usage timeline exposes 7-day 5h quota buckets

The account usage detail data MUST expose a 7-day quota timeline split into 5h buckets. Each bucket MUST include bucket start and end timestamps, primary-window usage volume for that bucket, weekly remaining percentage after the bucket, and whether a weekly reset is observed for that bucket.

#### Scenario: Account detail displays quota timeline

- **GIVEN** an account has quota timeline data
- **WHEN** the operator opens the account detail panel
- **THEN** the UI displays the 7-day 5h quota timeline with primary usage and weekly remaining series.

#### Scenario: Weekly reset nodes are visible

- **GIVEN** a weekly reset is observed in a timeline bucket
- **WHEN** the account usage timeline is rendered
- **THEN** the UI marks the reset node for that bucket.

### Requirement: Settings expose primary drain routing

The Settings routing strategy control MUST expose `primary_drain` as an operator-selectable strategy.

#### Scenario: Operator selects primary drain

- **WHEN** an operator selects Primary drain in Settings
- **THEN** the frontend saves `routingStrategy: "primary_drain"` to the settings API.
