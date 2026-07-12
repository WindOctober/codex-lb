## ADDED Requirements

### Requirement: Account quota timeline covers the current interval

The account detail quota timeline MUST include buckets through the current primary-window interval instead of stopping at the largest whole number of 5h buckets that fits inside seven days.

#### Scenario: operator views account quota timeline near now

- **WHEN** an account detail client requests the account trends payload
- **THEN** the returned quota timeline covers the 5h bucket containing the current server time
- **AND** the frontend renders quota timeline tick and tooltip timestamps in Asia/Taipei

### Requirement: Account quota timeline suppresses tiny reset tails

The account detail quota timeline MUST NOT render a reset-preceding primary-window tail shorter than one hour as a standalone bucket. Such a tail MUST be merged into the previous bucket while still starting the next bucket at the detected primary-window reset anchor.

#### Scenario: reset anchor leaves a 35-minute tail

- **WHEN** primary-window reset anchoring would create a reset-preceding bucket shorter than one hour
- **THEN** that short tail is merged into the previous bucket
- **AND** the next bucket starts at the reset anchor
